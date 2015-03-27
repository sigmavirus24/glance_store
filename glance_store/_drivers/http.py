# Copyright 2010 OpenStack Foundation
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import logging
import socket
import urlparse

import requests

from glance_store import capabilities
import glance_store.driver
from glance_store import exceptions
from glance_store.i18n import _
from glance_store.i18n import _LE
import glance_store.location

LOG = logging.getLogger(__name__)


MAX_REDIRECTS = 5


class StoreLocation(glance_store.location.StoreLocation):

    """Class describing an HTTP(S) URI."""

    def process_specs(self):
        self.scheme = self.specs.get('scheme', 'http')
        self.netloc = self.specs['netloc']
        self.user = self.specs.get('user')
        self.password = self.specs.get('password')
        self.path = self.specs.get('path')

    def _get_credstring(self):
        if self.user:
            return '%s:%s@' % (self.user, self.password)
        return ''

    def get_uri(self):
        return "%s://%s%s%s" % (
            self.scheme,
            self._get_credstring(),
            self.netloc,
            self.path)

    def parse_uri(self, uri):
        """
        Parse URLs. This method fixes an issue where credentials specified
        in the URL are interpreted differently in Python 2.6.1+ than prior
        versions of Python.
        """
        pieces = urlparse.urlparse(uri)
        assert pieces.scheme in ('https', 'http')
        self.scheme = pieces.scheme
        netloc = pieces.netloc
        path = pieces.path
        try:
            if '@' in netloc:
                creds, netloc = netloc.split('@')
            else:
                creds = None
        except ValueError:
            # Python 2.6.1 compat
            # see lp659445 and Python issue7904
            if '@' in path:
                creds, path = path.split('@')
            else:
                creds = None
        if creds:
            try:
                self.user, self.password = creds.split(':')
            except ValueError:
                reason = _("Credentials are not well-formatted.")
                LOG.info(reason)
                raise exceptions.BadStoreUri(message=reason)
        else:
            self.user = None
        if netloc == '':
            LOG.info(_("No address specified in HTTP URL"))
            raise exceptions.BadStoreUri(uri=uri)
        self.netloc = netloc
        self.path = path


def http_response_iterator(conn, response, size):
    """
    Return an iterator for a file-like object.

    :param conn: HTTP(S) Connection
    :param response: urllib3.HTTPResponse object
    :param size: Chunk size to iterate with
    """
    chunk = response.read(size)
    while chunk:
        yield chunk
        chunk = response.read(size)
    conn.close()


class Store(glance_store.driver.Store):

    """An implementation of the HTTP(S) Backend Adapter"""

    _CAPABILITIES = (capabilities.BitMasks.READ_ACCESS |
                     capabilities.BitMasks.DRIVER_REUSABLE)

    @capabilities.check
    def get(self, location, offset=0, chunk_size=None, context=None):
        """
        Takes a `glance_store.location.Location` object that indicates
        where to find the image file, and returns a tuple of generator
        (for reading the image file) and image_size

        :param location `glance_store.location.Location` object, supplied
                        from glance_store.location.get_location_from_uri()
        """
        conn, resp, content_length = self._query(location, 'GET')

        iterator = http_response_iterator(conn, resp, self.READ_CHUNKSIZE)

        class ResponseIndexable(glance_store.Indexable):
            def another(self):
                try:
                    return self.wrapped.next()
                except StopIteration:
                    return ''

        return (ResponseIndexable(iterator, content_length), content_length)

    def get_schemes(self):
        return ('http', 'https')

    def get_size(self, location, context=None):
        """
        Takes a `glance_store.location.Location` object that indicates
        where to find the image file, and returns the size

        :param location `glance_store.location.Location` object, supplied
                        from glance_store.location.get_location_from_uri()
        """
        try:
            size = self._query(location, 'HEAD')[2]
        except exceptions.BadStoreUri:
            raise
        except exceptions.NotFound:
            raise
        except Exception:
            # NOTE(flaper87): Catch more granular exceptions,
            # keeping this branch for backwards compatibility.
            return 0
        return size

    def _query(self, location, verb):
        redirects_followed = 0

        while redirects_followed < MAX_REDIRECTS:
            loc = location.store_location

            conn = self._get_response(loc, verb)

            # NOTE(sigmavirus24): If it was generally successful, break early
            if conn.status_code < 300:
                break

            self._check_store_uri(conn, loc)

            redirects_followed += 1

            # NOTE(sigmavirus24): Close the response so we don't leak sockets
            conn.close()

            location = self._new_location(location, conn.headers['location'])
        else:
            reason = (_("The HTTP URL exceeded %s maximum "
                        "redirects.") % MAX_REDIRECTS)
            LOG.debug(reason)
            raise exceptions.MaxRedirectsExceeded(message=reason)

        resp = conn.raw

        content_length = int(resp.getheader('content-length', 0))
        return (conn, resp, content_length)

    def _new_location(self, old_location, url):
        store_name = old_location.store_name
        store_class = old_location.store_location.__class__
        image_id = old_location.image_id
        store_specs = old_location.store_specs
        return glance_store.location.Location(store_name,
                                              store_class,
                                              self.conf,
                                              uri=url,
                                              image_id=image_id,
                                              store_specs=store_specs)

    def _check_store_uri(self, conn, loc):
        # TODO(sigmavirus24): Make this a staticmethod
        # Check for bad status codes
        if conn.status_code >= 400:
            if conn.status_code == requests.codes.not_found:
                reason = _("HTTP datastore could not find image at URI.")
                LOG.debug(reason)
                raise exceptions.NotFound(message=reason)

            reason = (_("HTTP URL %(url)s returned a "
                        "%(status)s status code.") %
                      {'url': loc.path, 'status': conn.status_code})
            LOG.debug(reason)
            raise exceptions.BadStoreUri(message=reason)

        if conn.is_redirect and conn.status_code not in (301, 302):
            reason = (_("The HTTP URL %(url)s attempted to redirect "
                        "with an invalid %(status)s status code.") %
                      {'url': loc.path, 'status': conn.status_code})
            LOG.info(reason)
            raise exceptions.BadStoreUri(message=reason)

    def _handle_connection_error(self, error):
        # TODO(sigmavirus24): Make this a staticmethod
        exc = error.args[0]
        if any(isinstance(e, socket.gaierror) for e in exc.args):
            reason = _LE("Remote server where the image is present "
                         "is unavailable.")
            LOG.error(reason)
            raise exceptions.RemoteServiceUnavailable(message=reason)
        else:
            reason = _("The HTTP URL is invalid.")
            LOG.info(reason)
            raise exceptions.BadStoreUri(message=reason)

    def _get_response(self, location, verb):
        if not hasattr(self, 'session'):
            self.session = requests.Session()

        try:
            return self.session.request(verb, location.get_uri(), stream=True,
                                        allow_redirects=False)
        except requests.exceptions.ConnectionError as err:
            self._handle_connection_error(err)
