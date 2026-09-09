# -*- coding: utf-8 -*-
"""Тесты REST-API фрагментов: создание, замена списком, комментарии, валидация.

Прогон:  cd backend && ./../venv/bin/python manage.py test
(или с системным python, где установлены django/djangorestframework).
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from vc_fragments.models import Fragment
from vc_pairs.models import VideoPair


class FragmentApiTestBase(APITestCase):
    def setUp(self):
        fake = SimpleUploadedFile("orig.mp4", b"fake-video-data", content_type="video/mp4")
        self.pair = VideoPair.objects.create(
            original=fake,
            original_name="test.mp4",
            total_frames=100,
            width=16,
            height=16,
            fps=30.0,
        )
        self.list_url = f"/api/v1/pairs/{self.pair.id}/fragments/"
        self.replace_url = f"/api/v1/pairs/{self.pair.id}/fragments/replace"


class CreateAndListTests(FragmentApiTestBase):
    def test_post_creates_fragment_with_comment(self):
        resp = self.client.post(
            self.list_url, {"start": 10, "end": 20, "comment": "первый"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        body = resp.json()
        self.assertEqual(body["start"], 10)
        self.assertEqual(body["end"], 20)
        self.assertEqual(body["comment"], "первый")

        frags = Fragment.objects.get()
        self.assertEqual(frags.comment, "первый")

    def test_post_default_comment_is_empty(self):
        resp = self.client.post(self.list_url, {"start": 5, "end": 9}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.json()["comment"], "")

    def test_post_overlap_rejected(self):
        self.client.post(self.list_url, {"start": 10, "end": 20}, format="json")
        resp = self.client.post(self.list_url, {"start": 15, "end": 25}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)

    def test_post_out_of_range_rejected(self):
        resp = self.client.post(self.list_url, {"start": 95, "end": 200}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_post_invalid_bounds_rejected(self):
        resp = self.client.post(self.list_url, {"start": 20, "end": 10}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_list_returns_comments(self):
        self.client.post(
            self.list_url, {"start": 10, "end": 20, "comment": "альфа"}, format="json"
        )
        self.client.post(
            self.list_url, {"start": 30, "end": 40, "comment": "бета"}, format="json"
        )
        resp = self.client.get(self.list_url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        data = resp.json()
        self.assertEqual(len(data), 2)
        self.assertEqual([f["comment"] for f in data], ["альфа", "бета"])


class ReplaceTests(FragmentApiTestBase):
    def test_replace_preserves_comments_roundtrip(self):
        sent = [
            {"start": 0, "end": 5, "comment": "первый кусок"},
            {"start": 20, "end": 30, "comment": "второй кусок"},
        ]
        resp = self.client.put(self.replace_url, sent, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        got = resp.json()
        self.assertEqual(len(got), 2)
        self.assertEqual([(f["start"], f["end"], f["comment"]) for f in got], [
            (0, 5, "первый кусок"),
            (20, 30, "второй кусок"),
        ])

        # Повторная замена без поля comment сохраняет комментарии (обратная
        # совместимость со старыми клиентами).
        resp2 = self.client.put(self.replace_url, [{"start": 0, "end": 5}], format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(resp2.json()[0]["comment"], "первый кусок")

    def test_replace_without_comment_key_fresh_pair(self):
        resp = self.client.put(self.replace_url, [{"start": 0, "end": 5}], format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()[0]["comment"], "")

    def test_replace_clears_comment_explicitly(self):
        self.client.put(
            self.replace_url, [{"start": 0, "end": 5, "comment": "было"}], format="json"
        )
        resp = self.client.put(
            self.replace_url, [{"start": 0, "end": 5, "comment": ""}], format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json()[0]["comment"], "")

    def test_replace_deletes_old_and_creates_new(self):
        self.client.put(
            self.replace_url,
            [{"start": 0, "end": 5, "comment": "старый"}],
            format="json",
        )
        resp = self.client.put(
            self.replace_url,
            [
                {"start": 10, "end": 20, "comment": "новый"},
                {"start": 40, "end": 50, "comment": "ещё один"},
            ],
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(Fragment.objects.count(), 2)
        self.assertEqual(resp.json()[0]["comment"], "новый")

    def test_replace_rejects_overlap(self):
        payload = [
            {"start": 10, "end": 30, "comment": "a"},
            {"start": 25, "end": 40, "comment": "b"},
        ]
        resp = self.client.put(self.replace_url, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)

    def test_replace_rejects_out_of_range(self):
        resp = self.client.put(
            self.replace_url, [{"start": 0, "end": 1000}], format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_replace_rejects_non_list(self):
        resp = self.client.put(self.replace_url, {"start": 0, "end": 5}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class DetailEmbedTests(FragmentApiTestBase):
    def test_pair_detail_embeds_comments(self):
        self.client.post(
            self.list_url, {"start": 10, "end": 20, "comment": "из списка"}, format="json"
        )
        resp = self.client.get(f"/api/v1/pairs/{self.pair.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        frags = resp.json()["fragments"]
        self.assertEqual(len(frags), 1)
        self.assertEqual(frags[0]["comment"], "из списка")


class DeleteTest(FragmentApiTestBase):
    def test_delete_removes_fragment(self):
        frag = Fragment.objects.create(
            video_pair=self.pair, start=10, end=20, comment="комментарий"
        )
        resp = self.client.delete(f"/api/v1/pairs/{self.pair.id}/fragments/{frag.id}")
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(Fragment.objects.count(), 0)