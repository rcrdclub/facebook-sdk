#!/usr/bin/env python
#
# Copyright 2010 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Python client library for the Facebook Platform.

This client library is designed to support the Graph API and the
official Facebook JavaScript SDK, which is the canonical way to
implement Facebook authentication. Read more about the Graph API at
http://developers.facebook.com/docs/api. You can download the Facebook
JavaScript SDK at http://github.com/facebook/connect-js/.

If your application is using Google AppEngine's webapp framework, your
usage of this module might look like this:

user = facebook.get_user_from_cookie(self.request.cookies, key, secret)
if user:
    graph = facebook.GraphAPI(user["access_token"])
    profile = graph.get_object("me")
    friends = graph.get_connections("me", "friends")

"""

import copy
import logging
import urllib
import hashlib
import hmac
import base64
import requests
import json
import time

# Find a query string parser
try:
    from urllib.parse import parse_qs
except ImportError:
    from urlparse import parse_qs


logger = logging.getLogger(__name__)


BASE_URL = "https://graph.facebook.com"
ERROR_CODE_TYPE_2 = 2


class GraphAPI(object):
    """A client for the Facebook Graph API.

    See http://developers.facebook.com/docs/api for complete
    documentation for the API.

    The Graph API is made up of the objects in Facebook (e.g., people,
    pages, events, photos) and the connections between them (e.g.,
    friends, photo tags, and event RSVPs). This client provides access
    to those primitive types in a generic way. For example, given an
    OAuth access token, this will fetch the profile of the active user
    and the list of the user's friends:

       graph = facebook.GraphAPI(access_token)
       user = graph.get_object("me")
       friends = graph.get_connections(user["id"], "friends")

    You can see a list of all of the objects and connections supported
    by the API at http://developers.facebook.com/docs/reference/api/.

    You can obtain an access token via OAuth or by using the Facebook
    JavaScript SDK. See
    http://developers.facebook.com/docs/authentication/ for details.

    If you are using the JavaScript SDK, you can use the
    get_user_from_cookie() method below to get the OAuth access token
    for the active user from the cookie saved by the SDK.

    """
    def __init__(self, access_token=None, timeout=None, base_url=None, follow_paging=True, error_code_2_retries=0, error_code_2_sleeptime=0):
        self.access_token = access_token
        self.timeout = timeout
        self.base_url = base_url or BASE_URL
        # Indicates whether you want your API requests to automatically do the
        # serial paging calls for you and return the aggregate results
        self.follow_paging = follow_paging
        # Sometimes we want to retry our requests when we get error code 2
        # (Temporary issue due to downtime - retry the operation after waiting.)
        # via https://developers.facebook.com/docs/graph-api/using-graph-api/
        self.error_code_2_retries = error_code_2_retries
        self.error_code_2_sleeptime = error_code_2_sleeptime
        self._batch_request = False

    def __enter__(self):
        self._batch_request = True
        self._requests_stack = []
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._batch_request = False

    def get_object(self, id, **args):
        """Fetchs the given object from the graph."""
        return self.request(id, args)

    def get_objects(self, ids, **args):
        """Fetchs all of the given object from the graph.

        We return a map from ID to object. If any of the IDs are
        invalid, we raise an exception.
        """
        args["ids"] = ",".join(ids)
        return self.request("", args)

    def get_connections(self, id, connection_name, **args):
        """Fetchs the connections for given object."""
        return self.request(id + "/" + connection_name, args)

    def post_object(self, id, **args):
        """Fetchs the given object from the graph, using POST.
        https://developers.facebook.com/docs/graph-api/using-graph-api/v2.3#largerequests
        """
        args["method"] = "GET"
        return self.request(id, args, method="POST")

    def post_objects(self, ids, **args):
        """Fetchs all of the given object from the graph, using POST.
        https://developers.facebook.com/docs/graph-api/using-graph-api/v2.3#largerequests

        We return a map from ID to object. If any of the IDs are
        invalid, we raise an exception.
        """
        args["method"] = "GET"
        args["ids"] = ",".join(ids)
        return self.request("", args, method="POST")

    def post_connections(self, id, connection_name, **args):
        """Fetchs the connections for given object, using POST.
        https://developers.facebook.com/docs/graph-api/using-graph-api/v2.3#largerequests
        """
        args["method"] = "GET"
        return self.request(id + "/" + connection_name, args, method="POST")

    def put_object(self, parent_object, connection_name, **data):
        """Writes the given object to the graph, connected to the given parent.

        For example,

            graph.put_object("me", "feed", message="Hello, world")

        writes "Hello, world" to the active user's wall. Likewise, this
        will comment on a the first post of the active user's feed:

            feed = graph.get_connections("me", "feed")
            post = feed["data"][0]
            graph.put_object(post["id"], "comments", message="First!")

        See http://developers.facebook.com/docs/api#publishing for all
        of the supported writeable objects.

        Certain write operations require extended permissions. For
        example, publishing to a user's feed requires the
        "publish_actions" permission. See
        http://developers.facebook.com/docs/publishing/ for details
        about publishing permissions.

        """
        assert self.access_token, "Write operations require an access token"
        return self.request(parent_object + "/" + connection_name,
                            post_args=data,
                            method="POST")

    def put_wall_post(self, message, attachment={}, profile_id="me"):
        """Writes a wall post to the given profile's wall.

        We default to writing to the authenticated user's wall if no
        profile_id is specified.

        attachment adds a structured attachment to the status message
        being posted to the Wall. It should be a dictionary of the form:

            {"name": "Link name"
             "link": "http://www.example.com/",
             "caption": "{*actor*} posted a new review",
             "description": "This is a longer description of the attachment",
             "picture": "http://www.example.com/thumbnail.jpg"}

        """
        return self.put_object(profile_id, "feed", message=message,
                               **attachment)

    def put_comment(self, object_id, message):
        """Writes the given comment on the given post."""
        return self.put_object(object_id, "comments", message=message)

    def put_like(self, object_id):
        """Likes the given post."""
        return self.put_object(object_id, "likes")

    def delete_object(self, id):
        """Deletes the object with the given ID from the graph."""
        self.request(id, method="DELETE")

    def delete_request(self, user_id, request_id):
        """Deletes the Request with the given ID for the given user."""
        self.request("%s_%s" % (request_id, user_id), method="DELETE")

    def put_photo(self, image, message=None, album_id=None, **kwargs):
        """Uploads an image using multipart/form-data.

        image=File like object for the image
        message=Caption for your image
        album_id=None posts to /me/photos which uses or creates and uses
        an album for your application.

        """
        object_id = album_id or "me"
        kwargs.update({"message": message})
        self.request(object_id,
                     post_args=kwargs,
                     files={"file": image},
                     method="POST")

    def _handle_response(self, status_code, headers, body, url=None):
        result = None
        try:
            # Attempt to retrieve JSON by default
            result = json.loads(body)
        except ValueError:
            if 'image/' in headers.get('content-type', ''):
                mimetype = headers['content-type']
                result = {"data": body,
                          "mime-type": mimetype,
                          "url": url}
            elif "access_token" in parse_qs(body):
                query_str = parse_qs(body)
                if "access_token" in query_str:
                    result = {"access_token": query_str["access_token"][0]}
                    if "expires" in query_str:
                        result["expires"] = query_str["expires"][0]
                else:
                    raise GraphAPIError(json.loads(body), status_code)
            else:
                raise GraphAPIError('Body was not JSON, image, or querystring',
                                    status_code)
        if status_code >= 400:
            if result:
                raise GraphAPIError(result, status_code)
            err_msg = "Received status_code %s but body type could not be determined" % status_code
            raise GraphAPIError(err_msg, status_code)
        if result and isinstance(result, dict) and result.get("error"):
            raise GraphAPIError(result, status_code)
        return result

    def request(
            self, path, args=None, post_args=None, files=None, method=None):
        """Fetches the given path in the Graph API.

        We translate args to a valid query string. If post_args is
        given, we send a POST request to the given path with the given
        arguments.

        """
        args = args or {}

        if self.access_token:
            if post_args is not None:
                post_args["access_token"] = self.access_token
            else:
                args["access_token"] = self.access_token
        method = method or "GET"

        if self._batch_request:
            # TODO: Support for binary data
            # https://developers.facebook.com/docs/graph-api/making-multiple-requests/#binary
            request = {'method': method}
            if method in ("POST", "PUT") and post_args:
                request['body'] = urllib.urlencode(post_args)
            if args:
                path += ('?' in path and '&' or '?')
                path += urllib.urlencode(args)
            logger.debug("Adding request (%s) to batch stack: %s", method, path)
            request['relative_url'] = path
            self._requests_stack.append(request)
            return

        url = self.base_url + '/' + path

        def _do_request_response():
            logger.debug("Request (%s) to %s", method, url)
            response = requests.request(method,
                                        url,
                                        timeout=self.timeout,
                                        params=args,
                                        data=post_args,
                                        files=files)
            return self._handle_response(response.status_code,
                                         response.headers,
                                         response.content,
                                         response.url)

        def _do_request_response_with_retries():
            try:
                return _do_request_response()
            except GraphAPIError as e:
                logger.warning("Caught GraphAPIError %s (type=%s)", e, e.type)
                if e.type != ERROR_CODE_TYPE_2 or not self.error_code_2_retries:
                    raise e
            logger.warning("Request resulted in error code 2, trying again %s time%s",
                           self.error_code_2_retries,
                           self.error_code_2_retries != 1 and 's' or '',
                           extra={'method': method, 'url': url})
            for attempt in xrange(1, self.error_code_2_retries + 1):
                logger.debug("Attempt %s (of %s)",
                                     attempt,
                                     self.error_code_2_retries)
                if self.error_code_2_sleeptime:
                    logger.debug("Sleeping for %s seconds before retrying after error code 2",
                                 self.error_code_2_sleeptime)
                    time.sleep(self.error_code_2_sleeptime)
                try:
                    return _do_request_response()
                except GraphAPIError as e:
                    if e.type != ERROR_CODE_TYPE_2 or attempt == self.error_code_2_retries:
                        raise e

        result = _do_request_response_with_retries()
        data = result.get('data') or []
        if self.follow_paging:
            pages_seen = 1
            next_result = copy.deepcopy(result)
            # If we do follow paging, don't return the paging data as part of
            # the result
            if 'paging' in result:
                del result['paging']
            while True:
                next_url = (next_result.get('paging') or {}).get('next')
                if not next_url:
                    break
                def _do_paged_request_response():
                    logger.debug("Paged request (%s) to %s", method, next_url)
                    response = requests.request(method,
                                                next_url,
                                                timeout=self.timeout)
                    return self._handle_response(response.status_code,
                                                 response.headers,
                                                 response.content,
                                                 response.url)

                def _do_paged_request_response_with_retries():
                    try:
                        return _do_paged_request_response()
                    except GraphAPIError as e:
                        logger.warning("Caught GraphAPIError %s (type=%s)", e, e.type)
                        if e.type != ERROR_CODE_TYPE_2 or not self.error_code_2_retries:
                            raise e
                    logger.warning("Paged request resulted in error code 2, trying again %s time%s",
                                   self.error_code_2_retries,
                                   self.error_code_2_retries != 1 and 's' or '',
                                   extra={'method': method, 'url': next_url})
                    for attempt in xrange(1, self.error_code_2_retries + 1):
                        logger.debug("Attempt %s (of %s)",
                                     attempt,
                                     self.error_code_2_retries)
                        if self.error_code_2_sleeptime:
                            logger.debug("Sleeping for %s seconds before retrying after error code 2",
                                         self.error_code_2_sleeptime)
                            time.sleep(self.error_code_2_sleeptime)
                        try:
                            return _do_paged_request_response()
                        except GraphAPIError as e:
                            if e.type != ERROR_CODE_TYPE_2 or attempt == self.error_code_2_retries:
                                raise e
                try:
                    next_result = _do_paged_request_response_with_retries()
                except GraphAPIError as e:
                    e.data = data
                    e.pages_seen = pages_seen
                    raise e
                data += (next_result.get('data') or [])
                pages_seen += 1
            if data:
                result.update({'data': data})
            result['pages_seen'] = pages_seen
        return result

    def execute(self):
        post_args = {'batch': json.dumps(self._requests_stack)}
        if self.access_token:
            post_args['access_token'] = self.access_token
        logger.debug("Batch request to %s with %s requests",
                     self.base_url,
                     len(self._requests_stack))
        try:
            batch_response = requests.post(self.base_url,
                                           post_args,
                                           timeout=self.timeout)
            batch_response.raise_for_status()
        except requests.HTTPError as e:
            response = getattr(e, 'response', None)
            if isinstance(response, requests.Response):
                error_data = None
                try:
                    # Best-effort attempt to extract more specific error data
                    error_data = response.json()
                except:
                    pass
                if error_data:
                    raise GraphAPIError(error_data)
            # Fallback to just creating a GraphAPIError obj out of `e`
            raise GraphAPIError(e)
        responses = []
        for response in batch_response.json():
            try:
                headers = {}
                for header in response.get('headers', []):
                    headers[header['name']] = header['value']
                result = self._handle_response(response['code'],
                                               headers,
                                               response['body'])
                responses.append(result)
            except Exception, e:
                responses.append(e)
        return responses


    def fql(self, query):
        """FQL query.

        Example query: "SELECT affiliations FROM user WHERE uid = me()"

        """
        self.request("fql", {"q": query})

    def get_app_access_token(self, app_id, app_secret):
        """Get the application's access token as a string."""
        args = {'grant_type': 'client_credentials',
                'client_id': app_id,
                'client_secret': app_secret}

        return self.request("oauth/access_token", args=args)["access_token"]

    def get_access_token_from_code(
            self, code, redirect_uri, app_id, app_secret):
        """Get an access token from the "code" returned from an OAuth dialog.

        Returns a dict containing the user-specific access token and its
        expiration date (if applicable).

        """
        args = {
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": app_id,
            "client_secret": app_secret}

        return self.request("oauth/access_token", args)

    def extend_access_token(self, app_id, app_secret):
        """
        Extends the expiration time of a valid OAuth access token. See
        <https://developers.facebook.com/roadmap/offline-access-removal/
        #extend_token>

        """
        args = {
            "client_id": app_id,
            "client_secret": app_secret,
            "grant_type": "fb_exchange_token",
            "fb_exchange_token": self.access_token}

        return self.request("oauth/access_token", args=args)

    def get_access_token_info(self, input_token=None):
        """
        Gets info about tokens/debugging

        When working with an access token, you may need to check what
        information is associated with it, such as its user or expiry. To get
        this information you can use our debug tool, or you can use the API
        endpoint.

        https://developers.facebook.com/docs/facebook-login/access-tokens#extending
        """
        if not input_token:
            # Default to the existing access token
            input_token = self.access_token
        args = {
            "input_token": input_token,
            "access_token": self.access_token}

        return self.request("debug_token", args)


class GraphAPIError(Exception):
    def __init__(self, result, status_code=None):
        self.result = result
        if status_code:
            self.type = status_code
        else:
            try:
                self.type = result["error_code"]
            except:
                self.type = ""

        # OAuth 2.0 Draft 10
        try:
            self.message = result["error_description"]
        except:
            # OAuth 2.0 Draft 00
            try:
                self.message = result["error"]["message"]
                self.type = result["error"]["code"]
            except:
                # REST server style
                try:
                    self.message = result["error_msg"]
                except:
                    self.message = result

        Exception.__init__(self, self.message)


def get_user_from_cookie(cookies, app_id, app_secret, call_facebook=True):
    """Parses the cookie set by the official Facebook JavaScript SDK.

    cookies should be a dictionary-like object mapping cookie names to
    cookie values.

    If the user is logged in via Facebook, we return a dictionary with
    the keys "uid" and "access_token". The former is the user's
    Facebook ID, and the latter can be used to make authenticated
    requests to the Graph API. If the user is not logged in, we
    return None.

    Download the official Facebook JavaScript SDK at
    http://github.com/facebook/connect-js/. Read more about Facebook
    authentication at
    http://developers.facebook.com/docs/authentication/.

    """
    cookie = cookies.get("fbsr_" + app_id, "")
    if not cookie:
        return None
    parsed_request = parse_signed_request(cookie, app_secret)
    if not parsed_request:
        return None
    if call_facebook:
        try:
            result = get_access_token_from_code(parsed_request["code"], "",
                                                app_id, app_secret)
        except GraphAPIError:
            return None
    else:
        result = {}
    result["uid"] = parsed_request["user_id"]
    return result


def parse_signed_request(signed_request, app_secret):
    """ Return dictionary with signed request data.

    We return a dictionary containing the information in the
    signed_request. This includes a user_id if the user has authorised
    your application, as well as any information requested.

    If the signed_request is malformed or corrupted, False is returned.

    """
    try:
        encoded_sig, payload = map(str, signed_request.split('.', 1))

        sig = base64.urlsafe_b64decode(encoded_sig + "=" *
                                       ((4 - len(encoded_sig) % 4) % 4))
        data = base64.urlsafe_b64decode(payload + "=" *
                                        ((4 - len(payload) % 4) % 4))
    except IndexError:
        # Signed request was malformed.
        return False
    except TypeError:
        # Signed request had a corrupted payload.
        return False

    data = json.loads(data)
    if data.get('algorithm', '').upper() != 'HMAC-SHA256':
        return False

    # HMAC can only handle ascii (byte) strings
    # http://bugs.python.org/issue5285
    app_secret = app_secret.encode('ascii')
    payload = payload.encode('ascii')

    expected_sig = hmac.new(app_secret,
                            msg=payload,
                            digestmod=hashlib.sha256).digest()
    if sig != expected_sig:
        return False

    return data


def auth_url(app_id, canvas_url, perms=None, **kwargs):
    url = "https://www.facebook.com/dialog/oauth?"
    kvps = {'client_id': app_id, 'redirect_uri': canvas_url}
    if perms:
        kvps['scope'] = ",".join(perms)
    kvps.update(kwargs)
    return url + urllib.urlencode(kvps)


def get_access_token_from_code(code, redirect_uri, app_id, app_secret):
    return GraphAPI().get_access_token_from_code(
        code, redirect_uri, app_id, app_secret)


def get_app_access_token(app_id, app_secret):
    return GraphAPI().get_app_access_token(app_id, app_secret)
