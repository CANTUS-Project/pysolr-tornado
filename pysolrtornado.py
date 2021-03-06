# -*- coding: utf-8 -*-
from __future__ import absolute_import, print_function, unicode_literals

import ast
import datetime
import logging
import os
import re
import socket
import time
# We can remove ExpatError when we drop support for Python 2.6:
from xml.parsers.expat import ExpatError

from tornado import gen, httpclient
from tornado import ioloop as ioloop_module
from tornado import log as tornado_log

try:
    from xml.etree import ElementTree as ET
except ImportError:
    try:
        from xml.etree import cElementTree as ET
    except ImportError:
        from xml.etree import ElementTree as ET

# Remove this when we drop Python 2.6:
ParseError = getattr(ET, 'ParseError', SyntaxError)

try:
    # Prefer simplejson, if installed.
    import simplejson as json
except ImportError:
    import json

try:
    # Python 3.X
    from urllib.parse import urlencode
except ImportError:
    # Python 2.X
    from urllib import urlencode

try:
    # Python 3.X
    import html.entities as htmlentities
except ImportError:
    # Python 2.X
    import htmlentitydefs as htmlentities

try:
    # Python 2.X
    unicode_char = unichr
except NameError:
    # Python 3.X
    unicode_char = chr
    # Ugh.
    long = int


__all__ = ['Solr']


def get_version():
    return "%s.%s.%s" % __version__[:3]


DATETIME_REGEX = re.compile(r'^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(\.\d+)?Z$')


class NullHandler(logging.Handler):
    def emit(self, record):
        pass


# set up logging with Tornado
LOG = tornado_log.app_log

# For debugging...
if os.environ.get("DEBUG_PYSOLR", "").lower() in ("true", "1"):
    LOG.setLevel(logging.DEBUG)
    stream = logging.StreamHandler()
    LOG.addHandler(stream)


def is_py3():
    try:
        basestring  # pylint: disable=pointless-statement
        return False
    except NameError:
        return True


IS_PY3 = is_py3()


def force_unicode(value):
    """
    Forces a bytestring to become a Unicode string.
    """
    if IS_PY3:
        # Python 3.X
        if isinstance(value, bytes):
            value = value.decode('utf-8', errors='replace')
        elif not isinstance(value, str):
            value = str(value)
    else:
        # Python 2.X
        if isinstance(value, str):
            value = value.decode('utf-8', 'replace')
        elif not isinstance(value, basestring):
            value = unicode(value)

    return value


def force_bytes(value):
    """
    Forces a Unicode string to become a bytestring.
    """
    if IS_PY3:
        if isinstance(value, str):
            value = value.encode('utf-8', 'backslashreplace')
    else:
        if isinstance(value, unicode):
            value = value.encode('utf-8')

    return value


def unescape_html(text):
    """
    Removes HTML or XML character references and entities from a text string.

    @param text The HTML (or XML) source text.
    @return The plain text, as a Unicode string, if necessary.

    Source: http://effbot.org/zone/re-sub.htm#unescape-html
    """
    def fixup(m):
        text = m.group(0)
        if text[:2] == "&#":
            # character reference
            try:
                if text[:3] == "&#x":
                    return unicode_char(int(text[3:-1], 16))
                else:
                    return unicode_char(int(text[2:-1]))
            except ValueError:
                pass
        else:
            # named entity
            try:
                text = unicode_char(htmlentities.name2codepoint[text[1:-1]])
            except KeyError:
                pass
        return text # leave as is
    return re.sub(r"&#?\w+;", fixup, text)


def safe_urlencode(params, doseq=0):
    """
    UTF-8-safe version of safe_urlencode

    The stdlib safe_urlencode prior to Python 3.x chokes on UTF-8 values
    which can't fail down to ascii.
    """
    if IS_PY3:
        return urlencode(params, doseq)

    if hasattr(params, "items"):
        params = params.items()

    new_params = list()

    for k, v in params:
        k = k.encode("utf-8")

        if isinstance(v, (list, tuple)):
            new_params.append((k, [force_bytes(i) for i in v]))
        else:
            new_params.append((k, force_bytes(v)))

    return urlencode(new_params, doseq)


def is_valid_xml_char_ordinal(i):
    """
    Defines whether char is valid to use in xml document

    XML standard defines a valid char as::

    Char ::= #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
    """
    return ( # conditions ordered by presumed frequency
        0x20 <= i <= 0xD7FF
        or i in (0x9, 0xA, 0xD)
        or 0xE000 <= i <= 0xFFFD
        or 0x10000 <= i <= 0x10FFFF
        )


def clean_xml_string(s):
    """
    Cleans string from invalid xml chars

    Solution was found there::

    http://stackoverflow.com/questions/8733233/filtering-out-certain-bytes-in-python
    """
    return ''.join(c for c in s if is_valid_xml_char_ordinal(ord(c)))


class SolrError(Exception):
    pass


class Results(object):
    """
    Default results class for wrapping decoded (from JSON) solr responses.

    Required ``decoded`` argument must be a Solr response dictionary. Individual documents can be
    retrieved either through the :attr:`Results.docs` attribute, through indexed access, or through
    iteration.

    Example::

        results = Results({
            'response': {
                'docs': [{'id': 1}, {'id': 2}, {'id': 3}],
                'numFound': 3,
            }
        })

        # You can iterate the "docs" by simply iterating the object itself, so this:
        for doc in results:
            print(str(doc))

        # ... is equivalent to this:
        for doc in results.docs:
            print(str(doc))

        # And these are equal too.
        list(results) == results.docs

        # You can also do indexed access.
        results[1] == results.docs[1]

        # You can also test for truth as you would expect:
        bool(results) == True

        # But with zero documents, it's false:
        bool(Results({})) == False

    **Additional Response Keys**

    The following additional data members are available:

    - hits
    - debug
    - highlighting
    - facets
    - spellcheck
    - stats
    - qtime
    - grouped
    - nextCursorMark

    You may add more attributes by extending the :class:`Results` class. For example:::

        class CustomResults(pysolrtornado.Results):
            def __init__(self, decoded):
                 super(self, CustomResults).__init__(decoded)
                 self.some_new_attribute = decoded.get('not_covered_key' None)
    """

    def __init__(self, decoded):
        # main response part of decoded Solr response
        response_part = decoded.get('response') or {}
        self.docs = response_part.get('docs', ())
        self.hits = int(response_part.get('numFound', 0))

        # other response metadata
        self.debug = decoded.get('debug', {})
        self.highlighting = decoded.get('highlighting', {})
        self.facets = decoded.get('facet_counts', {})
        self.spellcheck = decoded.get('spellcheck', {})
        self.stats = decoded.get('stats', {})
        self.qtime = decoded.get('responseHeader', {}).get('QTime', None)
        self.grouped = decoded.get('grouped', {})
        self.nextCursorMark = decoded.get('nextCursorMark', None)

    def __bool__(self):
        return self.hits > 0

    def __len__(self):
        return len(self.docs)

    def __iter__(self):
        return iter(self.docs)

    def __getitem__(self, i):
        return self.docs[i]


class Solr(object):
    """
    The main object for working with Solr.

    Optionally accepts ``decoder`` for an alternate JSON decoder instance.
    Default is ``json.JSONDecoder()``.

    Optionally accepts ``timeout`` for wait seconds until giving up on a
    request. Default is ``60`` seconds.

    Optionally accepts ``ioloop`` used for the AsyncHTTPClient. **But you should really include it
    because I don't know if it will work without being given that... TBD.**

    Optionally accepts ``results_cls`` that specifies class of results object
    returned by ``.search()`` and ``.more_like_this()`` methods.
    Default is ``pysolr.Results``.

    Usage::

        solr = pysolr.Solr('http://localhost:8983/solr')
        # With a 10 second timeout.

        solr = pysolr.Solr('http://localhost:8983/solr', timeout=10)

        # with a dict as a default results class instead of pysolr.Results
        solr = pysolr.Solr('http://localhost:8983/solr', results_cls=dict)

    """

    # Error messages for Solr._send_request()
    # They're class-level so they may be translated easier.
    _FETCH_VALUE_ERROR = 'URL is empty or protocol missing: {}'
    _FETCH_UNICODE_ERROR = 'URL is too long: {}'
    _FETCH_SOCKET_ERROR = 'Socket error (DNS?) connecting to {}'
    _FETCH_KEY_ERROR = 'Unknown HTTP method "{}"'
    _FETCH_CONN_ERROR = 'Connection error with {}'

    def __init__(self, url, decoder=None, timeout=None, ioloop=None, results_cls=None):
        self.decoder = decoder or json.JSONDecoder()
        self.url = url
        self.timeout = timeout or 60
        self.log = self._get_log()
        self._ioloop = ioloop or ioloop_module.IOLoop.instance()
        self._client = httpclient.AsyncHTTPClient(self._ioloop)
        self.results_cls = results_cls or Results

    def _get_log(self):
        return LOG

    def _create_full_url(self, path=''):
        if len(path):
            return '/'.join([self.url.rstrip('/'), path.lstrip('/')])

        # No path? No problem.
        return self.url

    @gen.coroutine
    def _send_request(self, method, path='', body=None, headers=None, files=None):
        url = self._create_full_url(path)
        method = method.upper()
        log_body = body

        if headers is None:
            headers = {}

        if log_body is None:
            log_body = ''
        elif not isinstance(log_body, str):
            log_body = repr(body)

        self.log.debug("Starting request to '%s' (%s) with body '%s'...",
                       url, method, log_body[:10])
        start_time = time.time()

        if files is not None:
            raise NotImplementedError('The "files" parameter in _send_request() does not work in Tornado yet')

        # actual Tornado request
        # Everything except the body can be Unicode. The body must be
        # encoded to bytes to work properly on Py3.
        bytes_body = body
        if bytes_body is not None:
            bytes_body = force_bytes(body)

        # prepare the request
        request = httpclient.HTTPRequest(url, method=method, headers=headers, body=bytes_body,
                                            request_timeout=self.timeout)

        try:
            # run the request
            resp = yield self._client.fetch(request)
        except UnicodeError:
            # when the URL is empty or too long or something
            # NOTE: must come before ValueError, since UnicodeError is a subclass of ValueError
            raise SolrError(Solr._FETCH_UNICODE_ERROR.format(url))
        except ValueError:
            # when the URL is empty or the HTTP/HTTPS part is missing
            raise SolrError(Solr._FETCH_VALUE_ERROR.format(url))
        except socket.gaierror:
            # DNS doesn't resolve or simlar
            raise SolrError(Solr._FETCH_SOCKET_ERROR.format(url))
        except KeyError:
            # unknown HTTP method
            raise SolrError(Solr._FETCH_KEY_ERROR.format(method))
        except ConnectionError:
            # could be various things
            raise SolrError(Solr._FETCH_CONN_ERROR.format(url))
        except httpclient.HTTPError as the_error:
            # Solr returned an error
            # TODO: this fails with a 599 (timeout, or something else when there was no HTTP response at all)
            error_message = '{}: {}'.format(the_error.code, the_error.response.reason)
            self.log.error(error_message, extra={'data': {'headers': the_error.response,
                                                          'response': the_error.response}})
            raise SolrError(error_message)

        end_time = time.time()
        self.log.info("Finished '%s' (%s) with body '%s' in %0.3f seconds.",
                      url, method, log_body[:10], end_time - start_time)

        return force_unicode(resp.body)

    @gen.coroutine
    def _select(self, params):
        # specify json encoding of results
        params['wt'] = 'json'
        params_encoded = safe_urlencode(params, True)

        if len(params_encoded) < 1024:
            # Typical case.
            path = 'select/?%s' % params_encoded
            return (yield self._send_request('get', path))
        else:
            # Handles very long queries by submitting as a POST.
            path = 'select/'
            headers = {
                'Content-type': 'application/x-www-form-urlencoded; charset=utf-8',
            }
            return (yield self._send_request('post', path, body=params_encoded, headers=headers))

    @gen.coroutine
    def _mlt(self, params):
        # specify json encoding of results
        params['wt'] = 'json'
        path = 'mlt/?%s' % safe_urlencode(params, True)
        return (yield self._send_request('get', path))

    @gen.coroutine
    def _suggest_terms(self, params):
        # specify json encoding of results
        params['wt'] = 'json'
        path = 'terms/?%s' % safe_urlencode(params, True)
        return (yield self._send_request('get', path))

    @gen.coroutine
    def _update(self, message, clean_ctrl_chars=True, commit=True, softCommit=False, waitFlush=None, waitSearcher=None):
        """
        Posts the given xml message to http://<self.url>/update and
        returns the result.

        Passing `sanitize` as False will prevent the message from being cleaned
        of control characters (default True). This is done by default because
        these characters would cause Solr to fail to parse the XML. Only pass
        False if you're positive your data is clean.
        """
        path = 'update/'

        # Per http://wiki.apache.org/solr/UpdateXmlMessages, we can append a
        # ``commit=true`` to the URL and have the commit happen without a
        # second request.
        query_vars = []

        if commit is not None:
            query_vars.append('commit=%s' % str(bool(commit)).lower())
        elif softCommit is not None:
            query_vars.append('softCommit=%s' % str(bool(softCommit)).lower())

        if waitFlush is not None:
            query_vars.append('waitFlush=%s' % str(bool(waitFlush)).lower())

        if waitSearcher is not None:
            query_vars.append('waitSearcher=%s' % str(bool(waitSearcher)).lower())

        if query_vars:
            path = '%s?%s' % (path, '&'.join(query_vars))

        # Clean the message of ctrl characters.
        if clean_ctrl_chars:
            message = sanitize(message)

        return (yield self._send_request('post', path, message, {'Content-type': 'text/xml; charset=utf-8'}))

    # TODO: convert to @staticmethod
    def _extract_error(self, resp):
        """
        Extract the actual error message from a solr response.
        """
        return '[Reason: {}]'.format(resp.reason)

    # TODO: convert to @staticmethod
    def _scrape_response(self, headers, response):
        """
        Scrape the html response.
        """
        # identify the responding server
        server_type = None
        server_string = headers.get('server', '')

        if server_string and 'jetty' in server_string.lower():
            server_type = 'jetty'

        if server_string and 'coyote' in server_string.lower():
            server_type = 'tomcat'

        reason = None
        full_html = ''
        dom_tree = None

        # In Python3, response can be made of bytes
        if IS_PY3 and hasattr(response, 'decode'):
            response = response.decode()
        if response.startswith('<?xml'):
            # Try a strict XML parse
            try:
                soup = ET.fromstring(response)

                reason_node = soup.find('lst[@name="error"]/str[@name="msg"]')
                tb_node = soup.find('lst[@name="error"]/str[@name="trace"]')
                if reason_node is not None:
                    full_html = reason = reason_node.text.strip()
                if tb_node is not None:
                    full_html = tb_node.text.strip()
                    if reason is None:
                        reason = full_html

                # Since we had a precise match, we'll return the results now:
                if reason and full_html:
                    return reason, full_html
            except (ParseError, ExpatError):
                # XML parsing error, so we'll let the more liberal code handle it.
                pass

        if server_type == 'tomcat':
            # Tomcat doesn't produce a valid XML response or consistent HTML:
            m = re.search(r'<(h1)[^>]*>\s*(.+?)\s*</\1>', response, re.IGNORECASE)
            if m:
                reason = m.group(2)
            else:
                full_html = "%s" % response
        else:
            # Let's assume others do produce a valid XML response
            try:
                dom_tree = ET.fromstring(response)
                reason_node = None

                # html page might be different for every server
                if server_type == 'jetty':
                    reason_node = dom_tree.find('body/pre')
                else:
                    reason_node = dom_tree.find('head/title')

                if reason_node is not None:
                    reason = reason_node.text

                if reason is None:
                    full_html = ET.tostring(dom_tree)
            except (SyntaxError, ExpatError):
                full_html = "%s" % response

        full_html = force_unicode(full_html)
        full_html = full_html.replace('\n', '')
        full_html = full_html.replace('\r', '')
        full_html = full_html.replace('<br/>', '')
        full_html = full_html.replace('<br />', '')
        full_html = full_html.strip()
        return reason, full_html

    # Conversion #############################################################

    # TODO: convert to @staticmethod
    def _from_python(self, value):
        """
        Converts python values to a form suitable for insertion into the xml
        we send to solr.
        """
        if hasattr(value, 'strftime'):
            if hasattr(value, 'hour'):
                value = "%sZ" % value.isoformat()
            else:
                value = "%sT00:00:00Z" % value.isoformat()
        elif isinstance(value, bool):
            if value:
                value = 'true'
            else:
                value = 'false'
        else:
            if IS_PY3:
                # Python 3.X
                if isinstance(value, bytes):
                    value = str(value, errors='replace')
            else:
                # Python 2.X
                if isinstance(value, str):
                    value = unicode(value, errors='replace')

            value = "{0}".format(value)

        return clean_xml_string(value)

    # TODO: convert to @staticmethod
    def _to_python(self, value):
        """
        Converts values from Solr to native Python values.
        """
        if isinstance(value, (int, float, long, complex)):
            return value

        if isinstance(value, (list, tuple)):
            value = value[0]

        if value == 'true':
            return True
        elif value == 'false':
            return False

        is_string = False

        if IS_PY3:
            if isinstance(value, bytes):
                value = force_unicode(value)

            if isinstance(value, str):
                is_string = True
        else:
            if isinstance(value, str):
                value = force_unicode(value)

            if isinstance(value, basestring):
                is_string = True

        if is_string == True:
            possible_datetime = DATETIME_REGEX.search(value)

            if possible_datetime:
                date_values = possible_datetime.groupdict()

                for dk, dv in date_values.items():
                    date_values[dk] = int(dv)

                return datetime.datetime(date_values['year'], date_values['month'], date_values['day'], date_values['hour'], date_values['minute'], date_values['second'])

        try:
            # This is slightly gross but it's hard to tell otherwise what the
            # string's original type might have been.
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            # If it fails, continue on.
            pass

        return value

    # TODO: convert to @staticmethod
    def _is_null_value(self, value):
        """
        Check if a given value is ``null``.

        Criteria for this is based on values that shouldn't be included
        in the Solr ``add`` request at all.
        """
        if value is None:
            return True

        if IS_PY3:
            # Python 3.X
            if isinstance(value, str) and len(value) == 0:
                return True
        else:
            # Python 2.X
            if isinstance(value, basestring) and len(value) == 0:
                return True

        # TODO: This should probably be removed when solved in core Solr level?
        return False

    # API Methods ############################################################

    @gen.coroutine
    def search(self, q, **kwargs):
        """
        Performs a search and returns the results.

        Requires a ``q`` for a string version of the query to run.

        Optionally accepts ``**kwargs`` for additional options to be passed
        through the Solr URL.

        Using the ``df`` keyword argument (specifying a default field) is strongly recommended, and
        indeed required for Solr 5.

        Returns ``self.results_cls`` class object (defaults to
        ``pysolr.Results``)

        Usage::

            # All docs.
            results = solr.search('*:*')

            # Search with highlighting.
            results = solr.search('ponies', **{
                'hl': 'true',
                'hl.fragsize': 10,
            })

        """
        params = {'q': q}
        params.update(kwargs)
        response = yield self._select(params)
        decoded = self.decoder.decode(response)

        self.log.debug(
            "Found '%s' search results.",
            # cover both cases: there is no response key or value is None
            (decoded.get('response', {}) or {}).get('numFound', 0)
        )
        return self.results_cls(decoded)

    @gen.coroutine
    def more_like_this(self, q, mltfl, **kwargs):
        """
        Finds and returns results similar to the provided query.

        Returns ``self.results_cls`` class object (defaults to
        ``pysolr.Results``)

        Requires Solr 1.3+.

        Usage::

            similar = solr.more_like_this('id:doc_234', 'text')

        """
        params = {
            'q': q,
            'mlt.fl': mltfl,
        }
        params.update(kwargs)
        response = yield self._mlt(params)
        decoded = self.decoder.decode(response)

        self.log.debug(
            "Found '%s' MLT results.",
            # cover both cases: there is no response key or value is None
            (decoded.get('response', {}) or {}).get('numFound', 0)
        )
        return self.results_cls(decoded)

    @gen.coroutine
    def suggest_terms(self, fields, prefix, **kwargs):
        """
        Accepts a list of field names and a prefix

        Returns a dictionary keyed on field name containing a list of
        ``(term, count)`` pairs

        Requires Solr 1.4+.
        """
        params = {
            'terms.fl': fields,
            'terms.prefix': prefix,
        }
        params.update(kwargs)
        response = yield self._suggest_terms(params)
        result = self.decoder.decode(response)
        terms = result.get("terms", {})
        res = {}

        # in Solr 1.x the value of terms is a flat list:
        #   ["field_name", ["dance",23,"dancers",10,"dancing",8,"dancer",6]]
        #
        # in Solr 3.x the value of terms is a dict:
        #   {"field_name": ["dance",23,"dancers",10,"dancing",8,"dancer",6]}
        if isinstance(terms, (list, tuple)):
            terms = dict(zip(terms[0::2], terms[1::2]))

        for field, values in terms.items():
            tmp = list()

            while values:
                tmp.append((values.pop(0), values.pop(0)))

            res[field] = tmp

        self.log.debug("Found '%d' Term suggestions results.", sum(len(j) for i, j in res.items()))
        return res

    # TODO: convert to @staticmethod
    def _build_doc(self, doc, boost=None, fieldUpdates=None):
        doc_elem = ET.Element('doc')

        for key, value in doc.items():
            if key == 'boost':
                doc_elem.set('boost', force_unicode(value))
                continue

            # To avoid multiple code-paths we'd like to treat all of our values as iterables:
            if isinstance(value, (list, tuple)):
                values = value
            else:
                values = (value, )

            for bit in values:
                if self._is_null_value(bit):
                    continue

                attrs = {'name': key}

                if fieldUpdates and key in fieldUpdates:
                    attrs['update'] = fieldUpdates[key]

                if boost and key in boost:
                    attrs['boost'] = force_unicode(boost[key])

                field = ET.Element('field', **attrs)
                field.text = self._from_python(bit)

                doc_elem.append(field)

        return doc_elem

    @gen.coroutine
    def add(self, docs, boost=None, fieldUpdates=None, commit=None, softCommit=None, commitWithin=None, waitFlush=None, waitSearcher=None):
        """
        Adds or updates documents.

        Requires ``docs``, which is a list of dictionaries. Each key is the
        field name and each value is the value to index.

        Optionally accepts ``commit``. Default is ``True``.

        Optionally accepts ``softCommit``. Default is ``False``.

        Optionally accepts ``boost``. Default is ``None``.

        Optionally accepts ``fieldUpdates``. Default is ``None``.

        Optionally accepts ``commitWithin``. Default is ``None``.

        Optionally accepts ``waitFlush``. Default is ``None``.

        Optionally accepts ``waitSearcher``. Default is ``None``.

        Usage::

            solr.add([
                {
                    "id": "doc_1",
                    "title": "A test document",
                },
                {
                    "id": "doc_2",
                    "title": "The Banana: Tasty or Dangerous?",
                },
            ])
        """
        commit = True if commit is None else commit
        softCommit = False if softCommit is None else softCommit

        start_time = time.time()
        self.log.debug("Starting to build add request...")
        message = ET.Element('add')

        if commitWithin:
            message.set('commitWithin', commitWithin)

        for doc in docs:
            message.append(self._build_doc(doc, boost=boost, fieldUpdates=fieldUpdates))

        # This returns a bytestring. Ugh.
        m = ET.tostring(message, encoding='utf-8')
        # Convert back to Unicode please.
        m = force_unicode(m)

        end_time = time.time()
        self.log.debug("Built add request of %s docs in %0.2f seconds.", len(message), end_time - start_time)
        return (yield self._update(m, commit=commit, softCommit=softCommit, waitFlush=waitFlush, waitSearcher=waitSearcher))

    @gen.coroutine
    def delete(self, id=None, q=None, commit=True, waitFlush=None, waitSearcher=None):  # pylint: disable=redefined-builtin
        """
        Deletes documents.

        Requires *either* ``id`` or ``query``. ``id`` is if you know the
        specific document id to remove. ``query`` is a Lucene-style query
        indicating a collection of documents to delete.

        Optionally accepts ``commit``. Default is ``True``.

        Optionally accepts ``waitFlush``. Default is ``None``.

        Optionally accepts ``waitSearcher``. Default is ``None``.

        Usage::

            solr.delete(id='doc_12')
            solr.delete(q='*:*')

        """
        if id is None and q is None:
            raise ValueError('You must specify "id" or "q".')
        elif id is not None and q is not None:
            raise ValueError('You many only specify "id" OR "q", not both.')
        elif id is not None:
            m = '<delete><id>%s</id></delete>' % id
        elif q is not None:
            m = '<delete><query>%s</query></delete>' % q

        return (yield self._update(m, commit=commit, waitFlush=waitFlush, waitSearcher=waitSearcher))

    @gen.coroutine
    def commit(self, softCommit=False, waitFlush=None, waitSearcher=None, expungeDeletes=None):
        """
        Forces Solr to write the index data to disk.

        Optionally accepts ``expungeDeletes``. Default is ``None``.

        Optionally accepts ``waitFlush``. Default is ``None``.

        Optionally accepts ``waitSearcher``. Default is ``None``.

        Optionally accepts ``softCommit``. Default is ``False``.

        Usage::

            solr.commit()

        """
        if expungeDeletes is not None:
            msg = '<commit expungeDeletes="%s" />' % str(bool(expungeDeletes)).lower()
        else:
            msg = '<commit />'

        return (yield self._update(msg, softCommit=softCommit, waitFlush=waitFlush, waitSearcher=waitSearcher))

    @gen.coroutine
    def optimize(self, waitFlush=None, waitSearcher=None, maxSegments=None):
        """
        Tells Solr to streamline the number of segments used, essentially a
        defragmentation operation.

        Optionally accepts ``maxSegments``. Default is ``None``.

        Optionally accepts ``waitFlush``. Default is ``None``.

        Optionally accepts ``waitSearcher``. Default is ``None``.

        Usage::

            solr.optimize()

        """
        if maxSegments:
            msg = '<optimize maxSegments="%d" />' % maxSegments
        else:
            msg = '<optimize />'

        return (yield self._update(msg, waitFlush=waitFlush, waitSearcher=waitSearcher))

    def extract(self, file_obj, extractOnly=True, **kwargs):
        """
        .. warning:: This method is not implemented yet in ``pysolr-tornado``.

        POSTs a file to the Solr ExtractingRequestHandler so rich content can
        be processed using Apache Tika. See the Solr wiki for details:

            http://wiki.apache.org/solr/ExtractingRequestHandler

        The ExtractingRequestHandler has a very simple model: it extracts
        contents and metadata from the uploaded file and inserts it directly
        into the index. This is rarely useful as it allows no way to store
        additional data or otherwise customize the record. Instead, by default
        we'll use the extract-only mode to extract the data without indexing it
        so the caller has the opportunity to process it as appropriate; call
        with ``extractOnly=False`` if you want to insert with no additional
        processing.

        Returns None if metadata cannot be extracted; otherwise returns a
        dictionary containing at least two keys:

            :contents:
                        Extracted full-text content, if applicable
            :metadata:
                        key:value pairs of text strings
        """
        raise NotImplementedError('extract() has not been ported to Tornado yet')
        #if not hasattr(file_obj, "name"):
            #raise ValueError("extract() requires file-like objects which have a defined name property")

        #params = {
            #"extractOnly": "true" if extractOnly else "false",
            #"lowernames": "true",
            #"wt": "json",
        #}
        #params.update(kwargs)

        #try:
            ## We'll provide the file using its true name as Tika may use that
            ## as a file type hint:
            #resp = self._send_request('post', 'update/extract',
                                      #body=params,
                                      #files={'file': (file_obj.name, file_obj)})
        #except (IOError, SolrError) as err:
            #self.log.error("Failed to extract document metadata: %s", err,
                           #exc_info=True)
            #raise

        #try:
            #data = json.loads(resp)
        #except ValueError as err:
            #self.log.error("Failed to load JSON response: %s", err,
                           #exc_info=True)
            #raise

        #data['contents'] = data.pop(file_obj.name, None)
        #data['metadata'] = metadata = {}

        #raw_metadata = data.pop("%s_metadata" % file_obj.name, None)

        #if raw_metadata:
            ## The raw format is somewhat annoying: it's a flat list of
            ## alternating keys and value lists
            #while raw_metadata:
                #metadata[raw_metadata.pop()] = raw_metadata.pop()

        #return data


class SolrCoreAdmin(object):
    """
    Handles core admin operations: see http://wiki.apache.org/solr/CoreAdmin

    Operations offered by Solr are:
       1. STATUS
       2. CREATE
       3. RELOAD
       4. RENAME
       5. ALIAS
       6. SWAP
       7. UNLOAD
       8. LOAD (not currently implemented)
    """
    def __init__(self, url, *args, **kwargs):
        super(SolrCoreAdmin, self).__init__(*args, **kwargs)
        self.url = url

    def _get_url(self, url, params=None, headers=None):
        params = {} if params is None else params
        headers = {} if headers is None else headers
        my_client = httpclient.HTTPClient()
        try:
            resp = my_client.fetch(httpclient.HTTPRequest(url,
                                                          headers=headers,
                                                          body=safe_urlencode(params),
                                                          allow_nonstandard_methods=True))
        finally:
            my_client.close()
        return force_unicode(resp.body)

    def status(self, core=None):
        """http://wiki.apache.org/solr/CoreAdmin#head-9be76f5a459882c5c093a7a1456e98bea7723953"""
        params = {
            'action': 'STATUS',
        }

        if core is not None:
            params.update(core=core)

        return self._get_url(self.url, params=params)

    def create(self, name, instance_dir=None, config='solrconfig.xml', schema='schema.xml'):
        """http://wiki.apache.org/solr/CoreAdmin#head-7ca1b98a9df8b8ca0dcfbfc49940ed5ac98c4a08"""
        params = {
            'action': 'CREATE',
            'name': name,
            'config': config,
            'schema': schema,
        }

        if instance_dir is None:
            params.update(instanceDir=name)
        else:
            params.update(instanceDir=instance_dir)

        return self._get_url(self.url, params=params)

    def reload(self, core):
        """http://wiki.apache.org/solr/CoreAdmin#head-3f125034c6a64611779442539812067b8b430930"""
        params = {
            'action': 'RELOAD',
            'core': core,
        }
        return self._get_url(self.url, params=params)

    def rename(self, core, other):
        """http://wiki.apache.org/solr/CoreAdmin#head-9473bee1abed39e8583ba45ef993bebb468e3afe"""
        params = {
            'action': 'RENAME',
            'core': core,
            'other': other,
        }
        return self._get_url(self.url, params=params)

    def swap(self, core, other):
        """http://wiki.apache.org/solr/CoreAdmin#head-928b872300f1b66748c85cebb12a59bb574e501b"""
        params = {
            'action': 'SWAP',
            'core': core,
            'other': other,
        }
        return self._get_url(self.url, params=params)

    def unload(self, core):
        """http://wiki.apache.org/solr/CoreAdmin#head-f5055a885932e2c25096a8856de840b06764d143"""
        params = {
            'action': 'UNLOAD',
            'core': core,
        }
        return self._get_url(self.url, params=params)

    def load(self, core):
        raise NotImplementedError('Solr 1.4 and below do not support this operation.')


# Using two-tuples to preserve order.
REPLACEMENTS = (
    # Nuke nasty control characters.
    (b'\x00', b''), # Start of heading
    (b'\x01', b''), # Start of heading
    (b'\x02', b''), # Start of text
    (b'\x03', b''), # End of text
    (b'\x04', b''), # End of transmission
    (b'\x05', b''), # Enquiry
    (b'\x06', b''), # Acknowledge
    (b'\x07', b''), # Ring terminal bell
    (b'\x08', b''), # Backspace
    (b'\x0b', b''), # Vertical tab
    (b'\x0c', b''), # Form feed
    (b'\x0e', b''), # Shift out
    (b'\x0f', b''), # Shift in
    (b'\x10', b''), # Data link escape
    (b'\x11', b''), # Device control 1
    (b'\x12', b''), # Device control 2
    (b'\x13', b''), # Device control 3
    (b'\x14', b''), # Device control 4
    (b'\x15', b''), # Negative acknowledge
    (b'\x16', b''), # Synchronous idle
    (b'\x17', b''), # End of transmission block
    (b'\x18', b''), # Cancel
    (b'\x19', b''), # End of medium
    (b'\x1a', b''), # Substitute character
    (b'\x1b', b''), # Escape
    (b'\x1c', b''), # File separator
    (b'\x1d', b''), # Group separator
    (b'\x1e', b''), # Record separator
    (b'\x1f', b''), # Unit separator
)

def sanitize(data):
    fixed_string = force_bytes(data)

    for bad, good in REPLACEMENTS:
        fixed_string = fixed_string.replace(bad, good)

    return force_unicode(fixed_string)
