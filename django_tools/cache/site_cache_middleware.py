# coding: utf-8

"""
    per-site cache middleware
    ~~~~~~~~~~~~~~~~~~~~~~~~~
    
    more information in the README.
    
    :copyleft: 2012 by the django-tools team, see AUTHORS for more details.
    :license: GNU GPL v3 or above, see LICENSE for more details.
"""

import sys
import logging

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import HttpResponse
from django.utils.cache import get_max_age, patch_response_headers
from django.utils.log import getLogger

from django_tools.utils.importlib import get_attr_from_settings



logger = getLogger("django-tools.CacheMiddleware")
logger.setLevel(logging.DEBUG)
#logger.addHandler(logging.StreamHandler())

CACHE_MIDDLEWARE_ANONYMOUS_ONLY = getattr(settings, 'CACHE_MIDDLEWARE_ANONYMOUS_ONLY', False)
RUN_WITH_DEV_SERVER = getattr(settings, "RUN_WITH_DEV_SERVER", "runserver" in sys.argv)
EXTRA_DEBUG = getattr(settings, "CACHE_EXTRA_DEBUG", False)

cache_callback = get_attr_from_settings("CACHE_CALLBACK", "DjangoTools cache callback")
logger.debug("Use cache callback: %s" % repr(cache_callback))


def build_cache_key(url, language_code, site_id):
    cache_key = "%s:%s:%s" % (url, language_code, site_id)
    if EXTRA_DEBUG:
        logger.debug("Cache key: %r" % cache_key)
    return cache_key


def get_cache_key(request):
    """
    Build the cache key based on the url and:
    
    * LANGUAGE_CODE: The language code in the url can be different than the
        used language for gettext translation.
    * SITE_ID: request.path is the url without the domain name. So the same
        url in site A and B would result in a collision.
    """
    url = request.get_full_path()

    try:
        language_code = request.LANGUAGE_CODE # set in django.middleware.locale.LocaleMiddleware
    except AttributeError:
        etype, evalue, etb = sys.exc_info()
        evalue = etype("%s (django.middleware.locale.LocaleMiddleware must be insert before cache middleware!)" % evalue)
        raise etype, evalue, etb

    site_id = settings.SITE_ID
    cache_key = build_cache_key(url, language_code, site_id)
    return cache_key


def delete_cache_item(url, language_code, site_id=None):
    if site_id is None:
        site_id = settings.SITE_ID

    cache_key = build_cache_key(url, language_code, site_id)
    logger.debug("delete from cache: %r" % cache_key)
    cache.delete(cache_key)


class CacheMiddlewareBase(object):
    def use_cache(self, request, response=None):
        if not request.method in ('GET', 'HEAD'):
            logger.debug("Don't cache %r (%s)" % (request.method, request.get_full_path()))
            return False

        if RUN_WITH_DEV_SERVER and request.path.startswith(settings.STATIC_URL):
            if EXTRA_DEBUG:
                logger.debug("Don't cache static files in dev server")
            return False

        if response and response.status_code != 200:
            logger.debug("Don't cache response with status code: %s (%s)" % (response.status_code, request.get_full_path()))
            return False

        if CACHE_MIDDLEWARE_ANONYMOUS_ONLY and request.user.is_authenticated():
            logger.debug("Don't cache requests from authenticated users.")
            return False

        if hasattr(request, '_messages') and len(request._messages) != 0:
            msg = "Don't cache: page for anonymous users has messages."
            if settings.DEBUG:
                storage = messages.get_messages(request)
                raw_messages = ", ".join([message.message for message in storage])
                storage.used = False
                msg += " (messages: %s)" % raw_messages
            logger.debug(msg)
            return False

        if response and getattr(response, 'csrf_processing_done', False):
            logger.debug("Don't cache because response.csrf_processing_done==True (e.g.: view use @csrf_protect decorator)")
            return False

        if cache_callback:
            return cache_callback(request, response)

        return True




class FetchFromCacheMiddleware(CacheMiddlewareBase):
    def process_request(self, request):
        """
        Try to fetch response from cache, if exists.
        """
        if not self.use_cache(request):
            if EXTRA_DEBUG:
                logger.debug("Don't fetch from cache: %s" % request.get_full_path())
            return

        cache_key = get_cache_key(request)
        response = cache.get(cache_key)
        if response is None:
            logger.debug("Not found in cache: %r" % cache_key)
        else:
            logger.debug("Use %r from cache!" % cache_key)
            response._from_cache = True
            return response


class UpdateCacheMiddleware(CacheMiddlewareBase):
    def process_response(self, request, response):
        if getattr(response, "_from_cache", False) == True:
            logger.debug("response comes from the cache, no need to update the cache")
            return response
        else:
            # used e.g. in unittests
            response._from_cache = False

        if not self.use_cache(request, response):
            if EXTRA_DEBUG:
                logger.debug("Don't put to cache: %s" % request.get_full_path())
            return response

        # get the timeout from the "max-age" section of the "Cache-Control" header
        timeout = get_max_age(response)
        if timeout == None:
            # use default cache_timeout
            timeout = settings.CACHE_MIDDLEWARE_SECONDS
        elif timeout == 0:
            logger.debug("Don't cache this page (timeout == 0)")
            return response

        # Create a new HttpResponse for the cache, so we can skip existing
        # cookies and attributes like response.csrf_processing_done
        response2 = HttpResponse(
            content=response._container,
            status=200,
            content_type=response['Content-Type'],
        )
        if response.has_header("Content-Language"):
            response2['Content-Language'] = response['Content-Language']

        if settings.DEBUG or RUN_WITH_DEV_SERVER or request.META.get('REMOTE_ADDR') in settings.INTERNAL_IPS:
            # Check if we store a {% csrf_token %} into the cache, this should never happen!
            for content in response._container:
                if "csrfmiddlewaretoken" in content:
                    raise AssertionError("csrf_token would be put into the cache! content: %r" % content)

        # Adds ETag, Last-Modified, Expires and Cache-Control headers
        patch_response_headers(response2, timeout)

        cache_key = get_cache_key(request)
        cache.set(cache_key, response2, timeout)

        logger.debug("Put to cache: %r" % cache_key)
        return response
