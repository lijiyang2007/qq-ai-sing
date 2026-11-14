"""Explicit original-label resolver policy; CPU/mock only."""
import json
import unittest
from unittest import mock
from hq_cover import song_resolver as resolver
from hq_cover import source


def track(track_id, album, origin=1, artist='Childish Gambino', title='Heartbeat'):
    return {
        'id': track_id,
        'name': title,
        'ar': [{'id': 1, 'name': artist}],
        'al': {'id': track_id + 100, 'name': album},
        'dt': 269840,
        'originCoverType': origin,
    }


def stream(track_id):
    return {
        'id': track_id, 'code': 200, 'level': 'lossless', 'type': 'flac',
        'size': 12345, 'md5': 'a' * 32,
        'url': 'https://m1.music.126.net/signed.flac',
    }


class ExplicitOriginalTests(unittest.TestCase):
    def resolve(self, rows, query='Heartbeat', counts=None, unavailable=()):
        session = mock.Mock()
        counts = counts or {}

        def api(_session, endpoint, args):
            if endpoint == '/api/cloudsearch/pc':
                return {'result': {'songs': rows, 'songCount': len(rows)}}
            if endpoint == '/api/song/red/count':
                count = counts.get(args['songId'])
                return {'code': 200, 'data': {'count': count, 'countDesc': 'irrelevant'}}
            if endpoint == '/api/song/detail':
                return {'songs': [row for row in rows if row['id'] == args['id']]}
            if endpoint == '/api/song/enhance/player/url/v1':
                track_id = json.loads(args['ids'])[0]
                return {'data': [stream(track_id) if track_id not in unavailable else {**stream(track_id), 'url': None}]}
            raise AssertionError(endpoint)

        with mock.patch.object(source, '_official_get', side_effect=api):
            return resolver.resolve_auto_original(
                query, catalog_provider=lambda **_kwargs: [],
                session_factory=lambda: session,
            )

    def test_selects_first_exact_explicit_original(self):
        cover = track(1, 'Cover Album', origin=2, artist='Cover Singer')
        original = track(2, 'Camp', origin=1)
        result = self.resolve([cover, original])
        self.assertTrue(result['ok'])
        self.assertEqual(result['track_id'], 2)
        self.assertEqual(result['selection_policy'], 'explicit_origin_cover_type_1_then_exact_studio_lossless_search_order')

    def test_excludes_live_remix_inexact_and_wrong_artist(self):
        rows = [
            track(1, 'Concert', origin=1, title='Heartbeat Live'),
            track(2, 'Remixes', origin=1, title='Heartbeat Remix'),
            track(3, 'Studio', origin=1, artist='Someone Else'),
            track(4, 'Studio', origin=1, title='Heartbeats'),
            track(5, 'Camp', origin=1),
        ]
        result = self.resolve(rows, 'Heartbeat Childish Gambino')
        self.assertTrue(result['ok'])
        self.assertEqual(result['track_id'], 5)

    def test_without_original_badge_selects_highest_exact_favorite_count(self):
        low = track(1, 'Studio A', origin=2)
        high = track(2, 'Studio B', origin=None)
        result = self.resolve([low, high], counts={1: 100, 2: 500})
        self.assertTrue(result['ok'])
        self.assertEqual(result['track_id'], 2)
        self.assertEqual(result['favorite_count'], 500)
        self.assertEqual(result['selection_policy'], 'no_original_badge_then_highest_exact_red_heart_count')

    def test_original_badge_wins_even_when_cover_has_more_favorites(self):
        original = track(1, 'Original', origin=1)
        popular_cover = track(2, 'Cover', origin=2)
        result = self.resolve([popular_cover, original], counts={1: 1, 2: 999999})
        self.assertTrue(result['ok'])
        self.assertEqual(result['track_id'], 1)
        self.assertNotIn('favorite_count', result)

    def test_favorite_count_unavailable_fails_closed_not_popularity(self):
        candidate = track(1, 'Studio', origin=2)
        candidate['pop'] = 100
        result = self.resolve([candidate], counts={})
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'favorite_count_unavailable')

    def test_top_favorite_without_lossless_does_not_silently_choose_second(self):
        high = track(1, 'Studio A', origin=2)
        low = track(2, 'Studio B', origin=2)
        result = self.resolve([high, low], counts={1: 500, 2: 100}, unavailable={1})
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'favorite_leader_unavailable')

    def test_title_text_never_substitutes_for_badge(self):
        fake = track(1, 'Studio', origin=2, title='Heartbeat (原唱)')
        result = self.resolve([fake], 'Heartbeat (原唱)', counts={1: 10})
        self.assertTrue(result['ok'])
        self.assertEqual(result['selection_policy'], 'no_original_badge_then_highest_exact_red_heart_count')
        real = track(2, 'Studio', origin=1, title='Heartbeat')
        result = self.resolve([real], 'Heartbeat')
        self.assertTrue(result['ok'])
        self.assertEqual(result['track_id'], 2)


if __name__ == '__main__':
    unittest.main()
