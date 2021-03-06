#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import json
import os.path
import re
import tempfile
import kerberos

from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import REDIRECT_FIELD_NAME, BACKEND_SESSION_KEY, authenticate, load_backend, login
from django.contrib.auth.middleware import RemoteUserMiddleware
from django.core import exceptions, urlresolvers
import django.db
from django.http import HttpResponseNotAllowed
from django.core.urlresolvers import resolve
from django.http import HttpResponseRedirect, HttpResponse
from django.utils.translation import ugettext as _
from django.utils.http import urlquote
from django.utils.encoding import iri_to_uri
import django.views.static
import django.views.generic.simple

import desktop.conf
from desktop.context_processors import get_app_name
from desktop.lib import apputil, i18n
from desktop.lib.django_util import render, render_json, is_jframe_request
from desktop.lib.exceptions import StructuredException
from desktop.lib.exceptions_renderable import PopupException
from desktop.log.access import access_log, log_page_hit
from desktop import appmanager
from hadoop import cluster
from desktop.log import get_audit_logger


LOG = logging.getLogger(__name__)

MIDDLEWARE_HEADER = "X-Hue-Middleware-Response"

# Views inside Django that don't require login
# (see LoginAndPermissionMiddleware)
DJANGO_VIEW_AUTH_WHITELIST = [
  django.views.static.serve,
  django.views.generic.simple.redirect_to,
]


class AjaxMiddleware(object):
  """
  Middleware that augments request to set request.ajax
  for either is_ajax() (looks at HTTP headers) or ?format=json
  GET parameters.
  """
  def process_request(self, request):
    request.ajax = request.is_ajax() or request.REQUEST.get("format", "") == "json"
    return None

class ExceptionMiddleware(object):
  """
  If exceptions know how to render themselves, use that.
  """
  def process_exception(self, request, exception):
    import traceback
    tb = traceback.format_exc()
    logging.info("Processing exception: %s: %s" % (i18n.smart_unicode(exception),
                                                   i18n.smart_unicode(tb)))

    if isinstance(exception, PopupException):
      return exception.response(request)

    if isinstance(exception, StructuredException):
      if request.ajax:
        response = render_json(exception.response_data)
        response[MIDDLEWARE_HEADER] = 'EXCEPTION'
        response.status_code = getattr(exception, 'error_code', 500)
        return response
      else:
        response = render("error.mako", request,
                      dict(error=exception.response_data.get("message")))
        response.status_code = getattr(exception, 'error_code', 500)
        return response

    return None

class JFrameMiddleware(object):
  """
  Updates JFrame headers to update path and push flash messages into headers.
  """
  def process_response(self, request, response):
    path = request.path
    if request.GET:
      get_params = request.GET.copy()
      if "noCache" in get_params:
        del get_params["noCache"]
      query_string = get_params.urlencode()
      if query_string:
        path = request.path + "?" + query_string
    response['X-Hue-JFrame-Path'] = iri_to_uri(path)
    if response.status_code == 200:
      if is_jframe_request(request):
        if hasattr(request, "flash"):
          flashes = request.flash.get()
          if flashes:
            response['X-Hue-Flash-Messages'] = json.dumps(flashes)

    return response

class ClusterMiddleware(object):
  """
  Manages setting request.fs and request.jt
  """
  def process_view(self, request, view_func, view_args, view_kwargs):
    """
    Sets request.fs and request.jt on every request to point to the
    configured filesystem.
    """
    request.fs_ref = request.REQUEST.get('fs', view_kwargs.get('fs', 'default'))
    if "fs" in view_kwargs:
      del view_kwargs["fs"]

    try:
      request.fs = cluster.get_hdfs(request.fs_ref)
    except KeyError:
      raise KeyError(_('Cannot find HDFS called "%(fs_ref)s".') % {'fs_ref': request.fs_ref})

    if request.user.is_authenticated():
      if request.fs is not None:
        request.fs.setuser(request.user.username)

      request.jt = cluster.get_default_mrcluster()
      if request.jt is not None:
        request.jt.setuser(request.user.username)
    else:
      request.jt = None


class NotificationMiddleware(object):
  """
  Manages setting request.info and request.error
  """
  def process_view(self, request, view_func, view_args, view_kwargs):

    def message(title, detail=None):
      if detail is None:
        detail = ''
      else:
        detail = '<br/>%s' % detail
      return '%s %s' % (title, detail)

    def info(title, detail=None):
      messages.info(request, message(title, detail))

    def error(title, detail=None):
      messages.error(request, message(title, detail))

    def warn(title, detail=None):
      messages.warning(request, message(title, detail))

    request.info = info
    request.error = error
    request.warn = warn


class AppSpecificMiddleware(object):
  @classmethod
  def augment_request_with_app(cls, request, view_func):
    """ Stuff the app into the request for use in later-stage middleware """
    if not hasattr(request, "_desktop_app"):
      module = apputil.getmodule_wrapper(view_func)
      request._desktop_app = apputil.get_app_for_module(module)
      if not request._desktop_app and not module.__name__.startswith('django.'):
        logging.debug("no app for view func: %s in %s" % (view_func, module))

  def __init__(self):
    self.middlewares_by_app = {}
    for app in appmanager.DESKTOP_APPS:
      self.middlewares_by_app[app.name] = self._load_app_middleware(app)

  def _get_middlewares(self, app, type):
    return self.middlewares_by_app.get(app, {}).get(type, [])

  def process_view(self, request, view_func, view_args, view_kwargs):
    """View middleware"""
    self.augment_request_with_app(request, view_func)
    if not request._desktop_app:
      return None

    # Run the middlewares
    ret = None
    for middleware in self._get_middlewares(request._desktop_app, 'view'):
      ret = middleware(request, view_func, view_args, view_kwargs)
      if ret: return ret # short circuit
    return ret

  def process_response(self, request, response):
    """Response middleware"""
    # We have the app that we stuffed in there
    if not hasattr(request, '_desktop_app'):
      logging.debug("No desktop_app known for request.")
      return response

    for middleware in reversed(self._get_middlewares(request._desktop_app, 'response')):
      response = middleware(request, response)
    return response

  def process_exception(self, request, exception):
    """Exception middleware"""
    # We have the app that we stuffed in there
    if not hasattr(request, '_desktop_app'):
      logging.debug("No desktop_app known for exception.")
      return None

    # Run the middlewares
    ret = None
    for middleware in self._get_middlewares(request._desktop_app, 'exception'):
      ret = middleware(request, exception)
      if ret: return ret # short circuit
    return ret

  def _load_app_middleware(cls, app):
    app_settings = app.settings
    if not app_settings:
      return
    mw_classes = app_settings.__dict__.get('MIDDLEWARE_CLASSES', [])

    result = {'view': [], 'response': [], 'exception': []}
    for middleware_path in mw_classes:
      # This code brutally lifted from django.core.handlers
      try:
          dot = middleware_path.rindex('.')
      except ValueError:
          raise exceptions.ImproperlyConfigured, _('%(module)s isn\'t a middleware module.') % {'module': middleware_path}
      mw_module, mw_classname = middleware_path[:dot], middleware_path[dot+1:]
      try:
          mod = __import__(mw_module, {}, {}, [''])
      except ImportError, e:
          raise exceptions.ImproperlyConfigured, _('Error importing middleware %(module)s: "%(error)s".') % {'module': mw_module, 'error': e}
      try:
          mw_class = getattr(mod, mw_classname)
      except AttributeError:
          raise exceptions.ImproperlyConfigured, _('Middleware module "%(module)s" does not define a "%(class)s" class.') % {'module': mw_module, 'class':mw_classname}

      try:
        mw_instance = mw_class()
      except exceptions.MiddlewareNotUsed:
        continue
      # End brutal code lift

      # We need to make sure we don't have a process_request function because we don't know what
      # application will handle the request at the point process_request is called
      if hasattr(mw_instance, 'process_request'):
        raise exceptions.ImproperlyConfigured, \
              _('AppSpecificMiddleware module "%(module)s" has a process_request function' + \
              ' which is impossible.') % {'module': middleware_path}
      if hasattr(mw_instance, 'process_view'):
        result['view'].append(mw_instance.process_view)
      if hasattr(mw_instance, 'process_response'):
        result['response'].insert(0, mw_instance.process_response)
      if hasattr(mw_instance, 'process_exception'):
        result['exception'].insert(0, mw_instance.process_exception)
    return result

class LoginAndPermissionMiddleware(object):
  """
  Middleware that forces all views (except those that opt out) through authentication.
  """
  def process_view(self, request, view_func, view_args, view_kwargs):
    """
    We also perform access logging in ``process_view()`` since we have the view function,
    which tells us the log level. The downside is that we don't have the status code,
    which isn't useful for status logging anyways.
    """
    access_log_level = getattr(view_func, 'access_log_level', None)
    # First, skip views not requiring login

    # If the view has "opted out" of login required, skip
    if hasattr(view_func, "login_notrequired"):
      log_page_hit(request, view_func, level=access_log_level or logging.DEBUG)
      return None

    # There are certain django views which are also opt-out, but
    # it would be evil to go add attributes to them
    if view_func in DJANGO_VIEW_AUTH_WHITELIST:
      log_page_hit(request, view_func, level=access_log_level or logging.DEBUG)
      return None

    # If user is logged in, check that he has permissions to access the
    # app.
    if request.user.is_active and request.user.is_authenticated():
      AppSpecificMiddleware.augment_request_with_app(request, view_func)

      # Until we get Django 1.3 and resolve returning the URL name, we just do a match of the name of the view
      try:
        access_view = 'access_view:%s:%s' % (request._desktop_app, resolve(request.path)[0].__name__)
      except Exception, e:
        access_log(request, 'error checking view perm: %s', e, level=access_log_level)
        access_view =''

      # Accessing an app can access an underlying other app.
      # e.g. impala or spark uses code from beeswax and so accessing impala shows up as beeswax here.
      # Here we trust the URL to be the real app we need to check the perms.
      app_accessed = request._desktop_app
      ui_app_accessed = get_app_name(request)
      if app_accessed != ui_app_accessed and ui_app_accessed not in ('logs', 'accounts', 'login'):
        app_accessed = ui_app_accessed

      if app_accessed and \
          app_accessed not in ("desktop", "home", "about") and \
          not (request.user.has_hue_permission(action="access", app=app_accessed) or
               request.user.has_hue_permission(action=access_view, app=app_accessed)):
        access_log(request, 'permission denied', level=access_log_level)
        return PopupException(
            _("You do not have permission to access the %(app_name)s application.") % {'app_name': app_accessed.capitalize()}, error_code=401).response(request)
      else:
        log_page_hit(request, view_func, level=access_log_level)
        return None

    logging.info("Redirecting to login page: %s", request.get_full_path())
    access_log(request, 'login redirection', level=access_log_level)
    if request.ajax:
      # Send back a magic header which causes Hue.Request to interpose itself
      # in the ajax request and make the user login before resubmitting the
      # request.
      response = HttpResponse("/* login required */", content_type="text/javascript")
      response[MIDDLEWARE_HEADER] = 'LOGIN_REQUIRED'
      return response
    else:
      return HttpResponseRedirect("%s?%s=%s" % (settings.LOGIN_URL, REDIRECT_FIELD_NAME, urlquote(request.get_full_path())))


class JsonMessage(object):
  def __init__(self, **kwargs):
    self.kwargs = kwargs

  def __str__(self):
    return json.dumps(self.kwargs)


class AuditLoggingMiddleware(object):

  def __init__(self):
    from desktop.conf import AUDIT_EVENT_LOG_DIR

    if not AUDIT_EVENT_LOG_DIR.get():
      LOG.info('Unloading AuditLoggingMiddleware')
      raise exceptions.MiddlewareNotUsed

  def process_response(self, request, response):
    try:
      audit_logger = get_audit_logger()
      audit_logger.debug(JsonMessage(**{
          datetime.utcnow().strftime('%s'): {
            'user': request.user.username  if hasattr(request, 'user') else 'anonymous',
            "status": response.status_code,
            "impersonator": None,
            "ip_address": request.META.get('REMOTE_ADDR'),
            "authorization_failure": response.status_code == 401,
            "service": get_app_name(request),
            "url": request.path,
          }
      }))
      response['audited'] = True
    except Exception, e:
      LOG.error('Could not audit the request: %s' % e)
    return response


class SessionOverPostMiddleware(object):
  """
  Django puts session info in cookies, which is reasonable.
  Unfortunately, the plugin we use for file-uploading
  doesn't forward the cookies, though it can do so over
  POST.  So we push the POST data back in.

  This is the issue discussed at
  http://www.stereoplex.com/two-voices/cookieless-django-sessions-and-authentication-without-cookies
  and
  http://digitarald.de/forums/topic.php?id=20

  The author of fancyupload says (http://digitarald.de/project/fancyupload/):
    Flash-request forgets cookies and session ID

    See option appendCookieData. Flash FileReference is not an intelligent
    upload class, the request will not have the browser cookies, Flash saves
    his own cookies. When you have sessions, append them as get-data to the the
    URL (e.g. "upload.php?SESSID=123456789abcdef"). Of course your session-name
    can be different.

  and, indeed, folks are whining about it: http://bugs.adobe.com/jira/browse/FP-78

  There seem to be some other solutions:
  http://robrosenbaum.com/flash/using-flash-upload-with-php-symfony/
  and it may or may not be browser and plugin-dependent.

  In the meanwhile, this is pretty straight-forward.
  """
  def process_request(self, request):
    cookie_key = settings.SESSION_COOKIE_NAME
    if cookie_key not in request.COOKIES and cookie_key in request.POST:
      request.COOKIES[cookie_key] = request.POST[cookie_key]
      del request.POST[cookie_key]


class DatabaseLoggingMiddleware(object):
  """
  If configured, logs database queries for every request.
  """
  DATABASE_LOG = logging.getLogger("desktop.middleware.DatabaseLoggingMiddleware")
  def process_response(self, request, response):
    if desktop.conf.DATABASE_LOGGING.get():
      if self.DATABASE_LOG.isEnabledFor(logging.INFO):
          # This only exists if desktop.settings.DEBUG is true, hence the use of getattr
          for query in getattr(django.db.connection, "queries", []):
            self.DATABASE_LOG.info("(%s) %s" % (query["time"], query["sql"]))
    return response


try:
  import tidylib
  _has_tidylib = True
except Exception, ex:
  # The exception type is not ImportError. It's actually an OSError.
  logging.warn("Failed to import tidylib (for debugging). Is libtidy installed?")
  _has_tidylib = False


class HtmlValidationMiddleware(object):
  """
  If configured, validate output html for every response.
  """
  def __init__(self):
    self._logger = logging.getLogger('HtmlValidationMiddleware')

    if not _has_tidylib:
      logging.error("HtmlValidationMiddleware not activatived: "
                    "Failed to import tidylib.")
      return

    # Things that we don't care about
    self._to_ignore = (
      re.compile('- Warning: <.*> proprietary attribute "data-'),
      re.compile('- Warning: trimming empty'),
      re.compile('- Info:'),
    )

    # Find the directory to write tidy html output
    try:
      self._outdir = os.path.join(tempfile.gettempdir(), 'hue_html_validation')
      if not os.path.isdir(self._outdir):
        os.mkdir(self._outdir, 0755)
    except Exception, ex:
      self._logger.exception('Failed to get temp directory: %s', (ex,))
      self._outdir = tempfile.mkdtemp(prefix='hue_html_validation-')

    # Options to pass to libtidy. See
    # http://tidy.sourceforge.net/docs/quickref.html
    self._options = {
      'show-warnings': 1,
      'output-html': 0,
      'output-xhtml': 1,
      'char-encoding': 'utf8',
      'output-encoding': 'utf8',
      'indent': 1,
      'wrap': 0,
    }


  def process_response(self, request, response):
    if not _has_tidylib or not self._is_html(request, response):
      return response

    html, errors = tidylib.tidy_document(response.content,
                                         self._options,
                                         keep_doc=True)
    if not errors:
      return response

    # Filter out what we care about
    err_list = errors.rstrip().split('\n')
    err_list = self._filter_warnings(err_list)
    if not err_list:
      return response

    try:
      fn = urlresolvers.resolve(request.path)[0]
      fn_name = '%s.%s' % (fn.__module__, fn.__name__)
    except:
      fn_name = '<unresolved_url>'

    # Write the two versions of html out for offline debugging
    filename = os.path.join(self._outdir, fn_name)

    result = "HTML tidy result: %s [%s]:" \
             "\n\t%s" \
             "\nPlease see %s.orig %s.tidy\n-------" % \
             (request.path, fn_name, '\n\t'.join(err_list), filename, filename)

    file(filename + '.orig', 'w').write(i18n.smart_str(response.content))
    file(filename + '.tidy', 'w').write(i18n.smart_str(html))
    file(filename + '.info', 'w').write(i18n.smart_str(result))

    self._logger.error(result)
    return response

  def _filter_warnings(self, err_list):
    """A hacky way to filter out things that we don't care about."""
    res = [ ]
    for err in err_list:
      for ignore in self._to_ignore:
        if ignore.search(err):
          break
      else:
        res.append(err)
    return res

  def _is_html(self, request, response):
    return not request.is_ajax() and \
        'html' in response['Content-Type'] and \
        200 <= response.status_code < 300

class SpnegoMiddleware(object):
  """
  Based on the WSGI SPNEGO middlware class posted here:
  http://code.activestate.com/recipes/576992/
  """

  def __init__(self):
    if not 'SpnegoDjangoBackend' in desktop.conf.AUTH.BACKEND.get():
      LOG.info('Unloading SpnegoMiddleware')
      raise exceptions.MiddlewareNotUsed

  def process_response(self, request, response):
    if 'GSS-String' in request.META:
      response['WWW-Authenticate'] = request.META['GSS-String']
    elif 'Return-401' in request.META:
      response = HttpResponse("401 Unauthorized", content_type="text/plain",
        status=401)
      response['WWW-Authenticate'] = 'Negotiate'
      response.status = 401
    return response

  def process_request(self, request):
    """
    The process_request() method needs to communicate some state to the
    process_response() method. The two options for this are to return an
    HttpResponse object or to modify the META headers in the request object. In
    order to ensure that all of the middleware is properly invoked, this code
    currently uses the later approach. The following headers are currently used:

    GSS-String:
      This means that GSS authentication was successful and that we need to pass
      this value for the WWW-Authenticate header in the response.

    Return-401:
      This means that the SPNEGO backend is in use, but we didn't get an
      AUTHORIZATION header from the client. The way that the protocol works
      (http://tools.ietf.org/html/rfc4559) is by having the first response to an
      un-authenticated request be a 401 with the WWW-Authenticate header set to
      Negotiate. This will cause the browser to re-try the request with the
      AUTHORIZATION header set.
    """
    # AuthenticationMiddleware is required so that request.user exists.
    if not hasattr(request, 'user'):
      raise ImproperlyConfigured(
        "The Django remote user auth middleware requires the"
        " authentication middleware to be installed.  Edit your"
        " MIDDLEWARE_CLASSES setting to insert"
        " 'django.contrib.auth.middleware.AuthenticationMiddleware'"
        " before the SpnegoUserMiddleware class.")

    if 'HTTP_AUTHORIZATION' in request.META:
      type, authstr = request.META['HTTP_AUTHORIZATION'].split(' ', 1)

      if type == 'Negotiate':
        try:
          result, context = kerberos.authGSSServerInit('HTTP')
          if result != 1:
            return

          gssstring=''
          r=kerberos.authGSSServerStep(context,authstr)
          if r == 1:
            gssstring=kerberos.authGSSServerResponse(context)
            request.META['GSS-String'] = 'Negotiate %s' % gssstring
          else:
            kerberos.authGSSServerClean(context)
            return

          username = kerberos.authGSSServerUserName(context)
          kerberos.authGSSServerClean(context)

          if request.user.is_authenticated():
            if request.user.username == self.clean_username(username, request):
              return

          user = authenticate(username=username)
          if user:
            request.user = user
            login(request, user)
          return
        except:
          LOG.exception('Unexpected error when authenticating against KDC')
          return
      else:
        request.META['Return-401'] = ''
        return
    else:
      if not request.user.is_authenticated():
        request.META['Return-401'] = ''
      return

  def clean_username(self, username, request):
    """
    Allows the backend to clean the username, if the backend defines a
    clean_username method.
    """
    backend_str = request.session[BACKEND_SESSION_KEY]
    backend = load_backend(backend_str)
    try:
      username = backend.clean_username(username)
    except AttributeError:
      pass
    return username


class HueRemoteUserMiddleware(RemoteUserMiddleware):
  """
  Middleware to delegate authentication to a proxy server. The proxy server
  will set an HTTP header (defaults to Remote-User) with the name of the
  authenticated user. This class extends the RemoteUserMiddleware class
  built into Django with the ability to configure the HTTP header and to
  unload the middleware if the RemoteUserDjangoBackend is not currently
  in use.
  """
  def __init__(self):
    if not 'RemoteUserDjangoBackend' in desktop.conf.AUTH.BACKEND.get():
      LOG.info('Unloading HueRemoteUserMiddleware')
      raise exceptions.MiddlewareNotUsed
    self.header = desktop.conf.AUTH.REMOTE_USER_HEADER.get()


class EnsureSafeMethodMiddleware(object):
  """
  Middleware to white list configured HTTP request methods.
  """
  def process_request(self, request):
    if request.method not in desktop.conf.HTTP_ALLOWED_METHODS.get():
      return HttpResponseNotAllowed(desktop.conf.HTTP_ALLOWED_METHODS.get())


class EnsureSafeRedirectURLMiddleware(object):
  """
  Middleware to white list configured redirect URLs.
  """
  def process_response(self, request, response):
    if response.status_code == 302:
      if any([regexp.match(response['Location']) for regexp in desktop.conf.REDIRECT_WHITELIST.get()]):
        return response

      response = render("error.mako", request, dict(error=_('Redirect to %s is not allowed.') % response['Location']))
      response.status_code = 403
      return response
    else:
      return response
