"""価格変更画面（S40〜S42）のテスト。"""

from decimal import Decimal

from django.urls import reverse

from bakery.models import AuditLog, CostSnapshot, Ingredient, Product, SnapshotReason
from bakery.tests.test_product_views import ProductTestBase


class PriceChangeTests(ProductTestBase):
    def setUp(self):
        super().setUp()
        self.dough, self.bread = self.make_final()
        self.register(recipe=self.bread)  # バターロール 200円・原価 22.26円
        self.product = Product.objects.get()
        self.client.force_login(self.staff)  # 価格変更は一般ユーザーもできる（第4項）
        self.url = reverse("price_change_edit", args=[self.flour.pk])

    def post(self, step, weight="25000", price="6000", **extra):
        return self.client.post(self.url, {"step": step, "purchase_weight_g": weight, "purchase_price": price, **extra})

    def test_select_and_input_pages(self):
        self.assertContains(self.client.get(reverse("price_change"), {"q": "強力"}), "強力粉")
        page = self.client.get(self.url)
        self.assertContains(page, 'value="25000"')  # 今の値が最初から入っている
        self.assertContains(page, "0.200 円/g")

    def test_live_unit_price_compare(self):
        response = self.client.post(reverse("price_change_check", args=[self.flour.pk]),
                                    {"purchase_weight_g": "25000", "purchase_price": "6000"})
        self.assertContains(response, "0.240 円/g")
        response = self.client.post(reverse("price_change_check", args=[self.flour.pk]),
                                    {"purchase_weight_g": "0", "purchase_price": "6000"})
        self.assertContains(response, "購入重量は1g以上で入力してください")

    def test_impact_then_commit(self):
        # 強力粉 5000円 → 6000円（0.2 → 0.24円/g）。生地 400円 → 440円、バターロール 22.26… → 24.48…円
        # 商品原価は小数第1位へ切り上げ：22.3円 → 24.5円
        preview = self.post("confirm")
        for text in ["0.200", "0.240", "+1,000.00 円", "22.3 円", "24.5 円", "+2.2 円", "11.2% → 12.3%", "パン生地"]:
            self.assertContains(preview, text)
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.purchase_price, Decimal("5000"))  # 確認画面ではまだ変えない

        response = self.post("commit", seen_weight="25000.00", seen_price="5000.00")
        self.assertRedirects(response, reverse("ingredient_detail", args=[self.flour.pk]))
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.purchase_price, Decimal("6000"))
        self.assertEqual(self.flour.updated_by, self.staff)
        latest = self.flour.price_history.first()
        self.assertEqual((latest.purchase_price, latest.changed_by), (Decimal("6000"), self.staff))
        log = AuditLog.objects.get(action="食材価格変更")
        self.assertEqual((log.before["purchase_price"], log.after["purchase_price"]), ("5000.00", "6000.00"))
        snap = CostSnapshot.objects.get(product=self.product, reason=SnapshotReason.PRICE_CHANGE)
        self.assertEqual(snap.cost, Decimal("24.5"))
        detail = self.client.get(reverse("product_detail", args=[self.product.pk]))
        self.assertContains(detail, "24.5 円")
        self.assertContains(detail, "食材価格変更")

    def test_back_keeps_input(self):
        response = self.post("back", price="5800")
        self.assertContains(response, 'value="5800"')

    def test_same_price_and_input_errors(self):
        self.assertContains(self.post("confirm", price="5000"), "今と同じです", status_code=422)
        self.assertContains(self.post("confirm", price=""), "購入価格を入力してください", status_code=422)
        self.assertContains(self.post("confirm", weight="0.5"), "購入重量は1g以上で入力してください", status_code=422)

    def test_stale_price_is_not_committed(self):
        # 確認画面を開いたあとに、別の人が 5500円 に変えた
        Ingredient.objects.filter(pk=self.flour.pk).update(purchase_price=Decimal("5500"))
        response = self.post("commit", seen_weight="25000.00", seen_price="5000.00")
        self.assertContains(response, "別の人がこの食材の価格を変更しました")
        self.assertContains(response, "5,500 円")  # 最新の価格で確認し直す
        self.flour.refresh_from_db()
        self.assertEqual(self.flour.purchase_price, Decimal("5500"))
        self.assertFalse(AuditLog.objects.filter(action="食材価格変更").exists())

    def test_weight_change_and_no_products(self):
        # 塩は商品に使われていない：影響なしでも変更できる。重量も変えられる
        url = reverse("price_change_edit", args=[self.salt.pk])
        preview = self.client.post(url, {"step": "confirm", "purchase_weight_g": "5000", "purchase_price": "400"})
        self.assertContains(preview, "この食材を使っている商品はありません")
        self.client.post(url, {"step": "commit", "purchase_weight_g": "5000", "purchase_price": "400",
                               "seen_weight": "1000.00", "seen_price": "100.00"})
        self.salt.refresh_from_db()
        self.assertEqual((self.salt.purchase_weight_g, self.salt.unit_price()), (Decimal("5000"), Decimal("0.08")))
