"""Tests for the layer cache key computed from a pipeline's configuration."""

from unittest import mock

import testtools

from occystrap.pipeline import PipelineBuilder
from occystrap import tarformat


class TestFiltersHash(testtools.TestCase):
    def test_default_pipeline_is_none(self):
        """No filters and gzip keeps the 'none' key."""
        self.assertEqual('none', PipelineBuilder._compute_filters_hash([]))

    def test_rewrite_version_changes_hash(self):
        """Layers rewritten by older rules are not reused."""
        filters = ['normalize-timestamps']
        before = PipelineBuilder._compute_filters_hash(filters)
        with mock.patch.object(tarformat, 'LAYER_REWRITE_VERSION',
                               tarformat.LAYER_REWRITE_VERSION + 1):
            after = PipelineBuilder._compute_filters_hash(filters)
        self.assertNotEqual(before, after)

    def test_rewrite_version_ignored_without_filters(self):
        """Recompression alone does not rewrite tar members."""
        before = PipelineBuilder._compute_filters_hash([], 'zstd')
        with mock.patch.object(tarformat, 'LAYER_REWRITE_VERSION',
                               tarformat.LAYER_REWRITE_VERSION + 1):
            after = PipelineBuilder._compute_filters_hash([], 'zstd')
        self.assertEqual(before, after)
