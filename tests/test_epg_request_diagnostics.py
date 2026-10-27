from __future__ import annotations

import gzip
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_match_inventory as matching
from epg_catalog_stream import stream_catalog_and_programmes_once


class RequestDiagnosticTests(unittest.TestCase):
    def result(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)/'source.xml.gz'
            source.write_bytes(gzip.compress(b'<tv><channel id="Alpha.in"/><channel id="ABC.in"/><channel id="abc.in"/></tv>'))
            return stream_catalog_and_programmes_once(
                path=source, fixed_wanted_ids=('alpha.IN','AbC.in'),
                select_provisional_ids=lambda _catalog: (),
                window_start=0, now_epoch=1790000000, minimum_unique_channels=1,
            )

    def row(self, server, stream, name, epg_id, enabled='TRUE'):
        return {'server_id':server,'stream_id':stream,'channel_name':name,
                'epg_id':epg_id,'enabled':enabled,'action':'APPROVED','source':'epgshare01','epg_feed':'ALL_SOURCES1'}

    def test_names_every_shared_channel_and_leaves_real_ambiguity_unresolved(self):
        result = self.result()
        rows = [self.row('server_1','10','Alpha HD','alpha.IN'),
                self.row('server_2','11','Alpha backup','alpha.IN'),
                self.row('server_3','12','Ambiguous channel','AbC.in'),
                self.row('server_1','13','Disabled','alpha.IN','FALSE')]
        with mock.patch('sys.stdout',new_callable=io.StringIO) as output:
            diagnostics = matching._request_resolution_diagnostics(result, rows)
        by_id = {row['requested_epg_id']:row for row in diagnostics}
        alias = by_id['alpha.IN']
        self.assertEqual(alias['canonical_epg_id'],'Alpha.in')
        self.assertEqual(alias['affected_channel_count'],2)
        self.assertEqual({row['stream_id'] for row in alias['affected_channels']},{'10','11'})
        unclear = by_id['AbC.in']
        self.assertEqual(unclear['status'],'ambiguous_unresolved')
        self.assertEqual(unclear['canonical_epg_id'],'')
        self.assertEqual(set(unclear['source_variants']),{'ABC.in','abc.in'})
        self.assertEqual(unclear['affected_channels'][0]['channel_name'],'Ambiguous channel')
        logged = json.loads(output.getvalue().split(': ',1)[1])
        self.assertEqual(logged['canonicalized_request_ids'],1)
        self.assertEqual(logged['ambiguous_request_ids'],1)

    def test_large_shared_group_is_bounded_and_urls_are_not_logged(self):
        result = self.result()
        rows = [self.row('server_1',str(i),'Alpha https://private.example/item','alpha.IN') for i in range(8)]
        with mock.patch('sys.stdout',new_callable=io.StringIO) as output:
            diagnostics = matching._request_resolution_diagnostics(result, rows)
        alias = next(row for row in diagnostics if row['requested_epg_id']=='alpha.IN')
        self.assertEqual(alias['affected_channel_count'],8)
        self.assertEqual(len(alias['affected_channels']),5)
        self.assertEqual(alias['affected_channels_omitted'],3)
        self.assertNotIn('private.example',output.getvalue())
        self.assertIn('[URL omitted]',output.getvalue())


if __name__ == '__main__':
    unittest.main()
