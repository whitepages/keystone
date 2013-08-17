# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 Metacloud
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from dogpile.cache import api
from dogpile.cache import proxy

from keystone.common import cache
from keystone.common import config
from keystone.tests import core as test


CONF = config.CONF
NO_VALUE = api.NO_VALUE
SHOULD_CACHE = cache.should_cache_fn('cache')


class TestProxy(proxy.ProxyBackend):
    def get(self, key):
        value = self.proxied.get(key)
        if value != NO_VALUE:
            value[0].cached = True
        return value


class TestProxyValue(object):
    def __init__(self, value):
        self.value = value
        self.cached = False


class CacheRegionTest(test.TestCase):
    def setUp(self):
        super(CacheRegionTest, self).setUp()
        self.region = cache.configure_cache_region(CONF)
        self.region.wrap(TestProxy)

    def test_region_built_with_proxy_direct_cache_test(self):
        """Verify cache regions are properly built with proxies."""
        test_value = TestProxyValue('Direct Cache Test')
        self.region.set('cache_test', test_value)
        cached_value = self.region.get('cache_test')
        self.assertTrue(cached_value.cached)

    def test_cache_region_no_error_multiple_config(self):
        """Verify configuring the CacheRegion again doesn't error."""
        cache.configure_cache_region(CONF, self.region)

    def test_should_cache_fn(self):
        """Verify should_cache_fn generates a sane function."""
        test_value = TestProxyValue('Decorator Test')

        @self.region.cache_on_arguments(should_cache_fn=SHOULD_CACHE)
        def cacheable_function(value):
            return value

        setattr(CONF.cache, 'caching', False)
        cacheable_function(test_value)
        cached_value = cacheable_function(test_value)
        self.assertFalse(cached_value.cached)

        setattr(CONF.cache, 'caching', True)
        cacheable_function(test_value)
        cached_value = cacheable_function(test_value)
        self.assertTrue(cached_value.cached)

    def test_cache_dictionary_config_builder(self):
        """Validate we build a sane dogpile.cache dictionary config."""
        CONF.cache.config_prefix = 'test_prefix'
        CONF.cache.backend = 'some_test_backend'
        CONF.cache.expiration_time = 86400
        CONF.cache.backend_argument = ['arg1:test', 'arg2:test:test',
                                       'arg3.invalid']

        config_dict = cache.build_cache_config(CONF)
        self.assertEquals(
            config_dict['test_prefix.backend'], CONF.cache.backend)
        self.assertEquals(
            config_dict['test_prefix.expiration_time'],
            CONF.cache.expiration_time)
        self.assertEquals(config_dict['test_prefix.arguments.arg1'], 'test')
        self.assertEquals(config_dict['test_prefix.arguments.arg2'],
                          'test:test')
        self.assertFalse('test_prefix.arguments.arg3' in config_dict)
