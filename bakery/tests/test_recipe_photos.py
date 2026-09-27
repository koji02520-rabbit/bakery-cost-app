"""レシピの写真（v24）のテスト。写真はテスト用の一時フォルダに保存する。"""

import io
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from PIL import Image

from bakery.models import AuditLog, Company, Membership, Product, ProductPhoto, Recipe, RecipePhoto
from bakery.tests.test_recipe_views import RecipeTestBase

TEMP_MEDIA = tempfile.mkdtemp(prefix="bakery-test-media-")


def make_image(name="photo.jpg", size=(3000, 2000), fmt="JPEG", exif_orientation=None, gps=False, mode="RGB"):
    image = Image.new(mode, size, (200, 150, 100) if mode == "RGB" else (200, 150, 100, 128))
    exif = Image.Exif()
    if exif_orientation:
        exif[0x0112] = exif_orientation  # Orientation
    if gps:
        exif[0x8825] = {1: "N", 2: (35.0, 41.0, 0.0)}  # GPSInfo（撮影場所）
    buffer = io.BytesIO()
    image.save(buffer, fmt, exif=exif) if fmt == "JPEG" else image.save(buffer, fmt)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=f"image/{fmt.lower()}")


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class RecipePhotoTests(RecipeTestBase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()  # 一般ユーザーでログイン（写真はレシピを編集できる人なら追加できる）
        self.recipe = self.create("パン生地")
        self.add(self.recipe, self.flour, 1000)

    def upload(self, *files, recipe=None):
        return self.client.post(reverse("recipe_photos_add", args=[(recipe or self.recipe).pk]), {"photos": list(files)})

    def test_upload_is_resized_rotated_and_stripped(self):
        response = self.upload(make_image(size=(3000, 2000), exif_orientation=6, gps=True))
        self.assertEqual(response.status_code, 200)
        photo = self.recipe.photos.get()
        with Image.open(photo.image.path) as saved:
            # 向き情報 6（右に90度）どおりに縦長へ回転し、長い辺を 1600px に縮めている
            self.assertEqual(saved.size, (1067, 1600))
            self.assertEqual(saved.format, "JPEG")
            self.assertEqual(len(saved.getexif()), 0)  # 撮影場所などの付帯情報は残さない
        with Image.open(photo.thumbnail.path) as thumb:
            self.assertEqual(max(thumb.size), 480)
        self.assertEqual(photo.created_by, self.staff)
        self.assertTrue(AuditLog.objects.filter(action="レシピ写真追加", target_id=self.recipe.pk).exists())
        self.assertContains(response, reverse("recipe_photo_file", args=[photo.pk, "thumb"]))

    def test_png_with_transparency_and_several_files(self):
        self.upload(make_image("a.png", (800, 600), "PNG", mode="RGBA"), make_image("b.jpg", (400, 300)))
        self.assertEqual(self.recipe.photos.count(), 2)
        first = self.recipe.photos.first()
        self.assertTrue(first.image.name.endswith(".jpg"))

    def test_not_an_image_is_rejected(self):
        bad = SimpleUploadedFile("memo.jpg", b"this is not an image", content_type="image/jpeg")
        response = self.upload(make_image(), bad)
        self.assertContains(response, "「memo.jpg」は画像として読み込めません")
        self.assertFalse(self.recipe.photos.exists())  # 1枚でも読めなければ、どれも保存しない

    def test_limit_per_recipe(self):
        for i in range(RecipePhoto.MAX_PHOTOS):
            RecipePhoto.objects.create(recipe=self.recipe, image="x.jpg", thumbnail="x.jpg", sort_order=i)
        self.assertContains(self.upload(make_image()), "20枚までです")

    def test_caption_move_and_delete(self):
        self.upload(make_image("1.jpg", (200, 100)), make_image("2.jpg", (200, 100)), make_image("3.jpg", (200, 100)))
        p1, p2, p3 = self.recipe.photos.all()
        self.client.post(reverse("recipe_photo_caption", args=[p3.pk]), {"caption": "  焼成後  "})
        p3.refresh_from_db()
        self.assertEqual(p3.caption, "焼成後")
        self.client.post(reverse("recipe_photo_move", args=[p3.pk]), {"direction": "first"})
        self.assertEqual(list(self.recipe.photos.values_list("pk", flat=True)), [p3.pk, p1.pk, p2.pk])
        self.client.post(reverse("recipe_photo_move", args=[p1.pk]), {"direction": "right"})
        self.assertEqual(list(self.recipe.photos.values_list("pk", flat=True)), [p3.pk, p2.pk, p1.pk])

        storage, name = p2.image.storage, p2.image.name
        self.assertTrue(storage.exists(name))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("recipe_photo_delete", args=[p2.pk]))
        self.assertFalse(RecipePhoto.objects.filter(pk=p2.pk).exists())
        self.assertFalse(storage.exists(name))  # ファイルも消える

    def test_in_use_recipe_can_change_photos(self):
        bread = self.create("バターロール", kind="final", recipe_type="")
        self.add(bread, self.recipe, 60)  # パン生地は使用中になる
        self.assertTrue(self.recipe.is_in_use())
        self.upload(make_image())
        self.assertEqual(self.recipe.photos.count(), 1)

    def test_copy_duplicates_photo_files(self):
        self.upload(make_image())
        self.client.post(reverse("recipe_photo_caption", args=[self.recipe.photos.get().pk]), {"caption": "完成"})
        url = self.client.post(reverse("recipe_new_version", args=[self.recipe.pk])).url
        v2 = Recipe.objects.get(pk=url.split("/")[2])
        original, copied = self.recipe.photos.get(), v2.photos.get()
        self.assertEqual(copied.caption, "完成")
        self.assertNotEqual(original.image.name, copied.image.name)
        # 元のレシピの写真を消しても、コピーの写真は残る
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("recipe_photo_delete", args=[original.pk]))
        self.assertTrue(copied.image.storage.exists(copied.image.name))
        self.assertEqual(self.client.get(reverse("recipe_photo_file", args=[copied.pk, "full"])).status_code, 200)

    def test_deleting_recipe_removes_files(self):
        draft = self.create("試作")
        self.add(draft, self.flour, 10)
        self.upload(make_image(), recipe=draft)
        photo = draft.photos.get()
        self.client.force_login(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("recipe_delete", args=[draft.pk]))
        self.assertFalse(photo.image.storage.exists(photo.image.name))

    def test_photo_file_access_control(self):
        self.upload(make_image())
        photo = self.recipe.photos.get()
        url = reverse("recipe_photo_file", args=[photo.pk, "thumb"])
        response = self.client.get(url)
        self.assertEqual((response.status_code, response["Content-Type"]), (200, "image/jpeg"))
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)  # ログインしていなければ見られない
        # ほかの会社のユーザーには見えない
        other_user = get_user_model().objects.create_user("other", password="x")
        other = Company.objects.create(name="他社")
        Membership.objects.create(user=other_user, company=other)
        self.client.force_login(other_user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(reverse("recipe_photo_delete", args=[photo.pk])).status_code, 404)
        self.assertTrue(RecipePhoto.objects.filter(pk=photo.pk).exists())

    def test_photos_shown_on_pages(self):
        self.upload(make_image())
        photo = self.recipe.photos.get()
        thumb = reverse("recipe_photo_file", args=[photo.pk, "thumb"])
        full = reverse("recipe_photo_file", args=[photo.pk, "full"])
        self.assertContains(self.client.get(reverse("recipe_edit", args=[self.recipe.pk])), thumb)
        detail = self.client.get(reverse("recipe_detail", args=[self.recipe.pk]))
        self.assertContains(detail, 'id="photo-panel"')  # 右上の写真欄
        self.assertContains(detail, thumb)
        self.assertContains(detail, full)
        self.assertContains(self.client.get(reverse("recipe_list")), thumb)


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class DetailPanelTests(RecipeTestBase):
    """詳細画面の右上の写真欄（v25）と、商品そのものの写真。"""

    def setUp(self):
        super().setUp()
        self.dough = self.create("パン生地")
        self.add(self.dough, self.flour, 1000)
        self.bread = self.create("バターロール", kind="final", recipe_type="")
        self.add(self.bread, self.dough, 60)
        self.product = Product.objects.create(company=self.company, code="ITEM-001", name="バターロール",
                                              price_excluding_tax=200, adopted_recipe=self.bread)

    def test_empty_panel_shows_add_tile(self):
        for url in (reverse("recipe_detail", args=[self.bread.pk]), reverse("product_detail", args=[self.product.pk])):
            with self.subTest(url=url):
                page = self.client.get(url)
                self.assertContains(page, 'id="photo-panel"')
                self.assertContains(page, "photo-add-tile")
                self.assertContains(page, "写真を追加")

    def test_add_from_recipe_detail_panel(self):
        response = self.client.post(reverse("recipe_photos_add", args=[self.bread.pk]),
                                    {"photos": [make_image()], "panel": "1"})
        self.assertContains(response, 'id="photo-panel"')  # 右上の欄だけを描き直す
        self.assertNotContains(response, 'id="photo-section"')
        self.assertEqual(self.bread.photos.count(), 1)

    def test_product_uses_recipe_photo_until_it_has_its_own(self):
        self.client.post(reverse("recipe_photos_add", args=[self.bread.pk]), {"photos": [make_image()]})
        recipe_photo = self.bread.photos.get()
        page = self.client.get(reverse("product_detail", args=[self.product.pk]))
        self.assertContains(page, reverse("recipe_photo_file", args=[recipe_photo.pk, "thumb"]))
        self.assertContains(page, "レシピの写真")

        # 一般ユーザーも商品の写真を追加できる（写真は原価に影響しない）
        response = self.client.post(reverse("product_photos_add", args=[self.product.pk]),
                                    {"photos": [make_image()], "panel": "1"})
        product_photo = self.product.photos.get()
        self.assertEqual(product_photo.created_by, self.staff)
        self.assertTrue(product_photo.image.name.startswith(f"product_photos/{self.company.pk}/"))
        self.assertContains(response, reverse("product_photo_file", args=[product_photo.pk, "thumb"]))
        self.assertNotContains(response, "レシピの写真")
        self.assertTrue(AuditLog.objects.filter(action="商品写真追加", target_id=self.product.pk).exists())

    def test_product_photo_management_page(self):
        self.client.post(reverse("product_photos_add", args=[self.product.pk]), {"photos": [make_image(), make_image()]})
        p1, p2 = self.product.photos.all()
        page = self.client.get(reverse("product_photos", args=[self.product.pk]))
        self.assertContains(page, 'id="photo-section"')
        self.client.post(reverse("product_photo_move", args=[p2.pk]), {"direction": "first"})
        self.assertEqual(self.product.photos.first(), p2)
        self.client.post(reverse("product_photo_caption", args=[p2.pk]), {"caption": "店頭用"})
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("product_photo_delete", args=[p1.pk]))
        self.assertEqual(list(self.product.photos.values_list("caption", flat=True)), ["店頭用"])
        self.assertFalse(p1.image.storage.exists(p1.image.name))
        detail = self.client.get(reverse("product_detail", args=[self.product.pk]))
        self.assertContains(detail, "写真の編集（1枚）")

    def test_product_photo_access_control(self):
        self.client.post(reverse("product_photos_add", args=[self.product.pk]), {"photos": [make_image()]})
        photo = self.product.photos.get()
        url = reverse("product_photo_file", args=[photo.pk, "full"])
        self.assertEqual(self.client.get(url).status_code, 200)
        other_user = get_user_model().objects.create_user("other2", password="x")
        Membership.objects.create(user=other_user, company=Company.objects.create(name="他社2"))
        self.client.force_login(other_user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(reverse("product_photos", args=[self.product.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("product_photos_add", args=[self.product.pk]),
                                          {"photos": [make_image()]}).status_code, 404)
        self.assertEqual(ProductPhoto.objects.count(), 1)
