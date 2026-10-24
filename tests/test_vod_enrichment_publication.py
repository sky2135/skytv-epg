import io
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_skytvplayer_catalog as app
import enrich_vod_catalog as enrichment
import vod_enrichment_artifacts as artifacts
import vod_tmdb as tmdb
from test_vod_tmdb import FakeResponse, FakeSession, movie_details


class IndependentPublicationTests(unittest.TestCase):
    def test_worker_cache_archive_supplies_dates_and_genres_without_publisher_http(self):
        examples = [('954024', 335977, 'Indiana Jones and the Dial of Destiny',
                     'EN - Indiana Jones and the Dial of Destiny (2023) 4K', '2023-06-25'),
                    ('955380', 346698, 'Barbie', 'EN - Barbie 4K (2023)', '2023-07-19')]
        details = {identifier: {**movie_details(identifier), 'title': title,
                   'original_title': title, 'release_date': release, 'original_language': 'en'}
                   for _provider, identifier, title, _display, release in examples}

        class OfflineSession(FakeSession):
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return FakeResponse(details[int(url.rsplit('/', 1)[-1])])

        source = {'get_vod_categories': [{'category_id': '10', 'category_name': 'English Movies'}],
                  'get_vod_streams': [{'stream_id': provider, 'name': display, 'tmdb': str(identifier),
                     'category_id': '10', 'is_adult': '0', 'container_extension': 'mkv',
                     'stream_icon': 'https://image.tmdb.org/t/p/w500/abc123.jpg'}
                     for provider, identifier, _title, display, _release in examples],
                  'get_series_categories': [], 'get_series': [], 'vod_details': {}, 'series_details': {}}
        prepared = app._prepare_catalog('server_3', 'movie', source, None)
        records = [{'contentId': item['contentId'], 'serverId': 'server_3', 'kind': 'movie',
                    'languages': ['en'], 'item': app._matching_item(item, prepared['raw_rows'][item['id']], 'movie', None),
                    'baseline': {'metadataMatch': 'unavailable', 'tmdbId': None}}
                   for item in prepared['normalized']]
        document = {'schemaVersion': 1, 'recordCount': 2, 'inputFingerprint': 'f' * 64, 'records': records}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'state'
            state.mkdir()
            artifacts.atomic_json(state / 'control.json', {'schemaVersion': 1, 'isolated': False, 'sourceArtifactId': 123})
            with mock.patch.object(enrichment, 'load_component_input', return_value=document):
                report = enrichment.run_batch(state_dir=state, component_dir=root / 'component',
                    token='offline', workers=1, batch_seconds=10, session_factory=lambda _limiter: OfflineSession())
            self.assertIsNone(report['fatalError'])
            self.assertEqual(report['totals']['matched'], 2)
            artifacts.seal(state)
            candidate = root / 'candidate'
            self.assertEqual(artifacts.export_cache(state, candidate)['positive'], 2)
            contents = io.BytesIO()
            with tarfile.open(fileobj=contents, mode='w') as packed:
                for path in candidate.rglob('*'):
                    if path.is_file():
                        packed.add(path, arcname=path.relative_to(candidate).as_posix())
            archive = root / 'candidate.zip'
            with ZipFile(archive, 'w') as packed:
                # Match the actual workflow, including its distinct cache tar name.
                packed.writestr('cache.tar', contents.getvalue())
            github = mock.Mock()
            github.download.side_effect = lambda _artifact, destination: shutil.copyfile(archive, destination)
            cache = root / 'publisher-cache'
            with mock.patch.object(artifacts, '_latest', return_value={'id': 456}):
                imported = artifacts.import_cache(cache, github=github)
            self.assertEqual(imported['imported'], 2)
            session = FakeSession()  # Any attempted HTTP request fails this test.
            with tmdb.TmdbClient(cache / 'tmdb', max_requests=0, session=session) as client:
                index = app.build(root / 'app', cache, [('server_3', source, None)], client,
                                  '2026-10-24T16:00:00Z')
                self.assertEqual(client.stats['requests'], 0)
            manifest = app.read_artifact(root / 'app', index['servers'][0]['movies'])
            rows = [row for shard in manifest['shards'] for row in app.read_artifact(root / 'app', shard)['items']]
            self.assertEqual({row['id'] for row in rows}, {'954024', '955380'})
            for row in rows:
                expected = next(example for example in examples if example[0] == row['id'])
                self.assertEqual(row['tmdbId'], str(expected[1]))
                self.assertEqual(row['releaseDate'], expected[4])
                self.assertEqual(row['genres'], ['drama'])
                self.assertEqual(row['metadataMatch'], 'matched_id')
            self.assertEqual(session.calls, [])

    def test_workflow_keeps_api_work_separate_and_seals_before_continuing(self):
        root = Path(__file__).resolve().parents[1]
        publisher = (root / '.github/workflows/vod.yml').read_text()
        worker = (root / '.github/workflows/enrich-vod.yml').read_text()
        self.assertIn('actions: read', publisher)
        self.assertIn('--max-tmdb-requests 0', publisher)
        self.assertNotIn('--backfill-release-dates', publisher)
        build_step = publisher.split('- name: Build SKYTVPlayer movie and TV home database', 1)[1].split('      - name:', 1)[0]
        self.assertNotIn('TMDB_READ_ACCESS_TOKEN:', build_step)
        self.assertNotIn('publish-component.yml', worker)
        self.assertNotIn('deploy-pages@', worker)
        self.assertLess(worker.index('Retain the sealed checkpoint'), worker.index('Continue due work'))
        self.assertLess(worker.index('Retain the sealed checkpoint'), worker.index('Report a failed batch'))


if __name__ == '__main__':
    unittest.main()
