"""初期データの登録。何度実行しても重複しない。

使い方:
    python manage.py setup_initial_data --company "会社名" --store "店舗名" --admin ユーザー名
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from bakery.models import (
    AllergenItem,
    AllergenKind,
    Company,
    IdSequence,
    IngredientCategory,
    Membership,
    Role,
    Store,
    Supplier,
    SystemSetting,
)

# spec.md 第7項。S.自社製品・U.生地は中間レシピとして扱うため食材カテゴリーにしない（第7項 v5）
CATEGORIES = [
    ("A", "粉"), ("B", "穀物・豆"), ("C", "砂糖"), ("D", "はちみつ・メープル"), ("E", "塩"),
    ("F", "ジュース・酒類"), ("G", "スパイス類・香料"), ("H", "青果・卵"), ("I", "製パン・製菓材料"),
    ("J", "チョコレート"), ("K", "ナッツ・シード"), ("L", "乳製品・油脂"), ("M", "抹茶・紅茶・茶"),
    ("N", "調味料"), ("O", "缶詰・瓶"), ("P", "肉・魚介・冷凍野菜"), ("Q", "水"), ("R", "半製品"),
]

# spec.md 第20項 v12（2026年4月1日施行の食品表示基準改正後）。実装時に消費者庁の一次情報で再確認すること
MANDATORY = ["えび", "かに", "くるみ", "小麦", "そば", "卵", "乳", "落花生（ピーナッツ）", "カシューナッツ"]
RECOMMENDED = [
    "アーモンド", "あわび", "いか", "いくら", "オレンジ", "キウイフルーツ", "牛肉", "ごま", "さけ", "さば",
    "大豆", "鶏肉", "バナナ", "豚肉", "マカダミアナッツ", "もも", "やまいも", "りんご", "ゼラチン", "ピスタチオ",
]


class Command(BaseCommand):
    help = "会社・店舗・食材カテゴリー・アレルゲン項目・仕入先「自社」を登録する"

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="会社名")
        parser.add_argument("--store", required=True, help="店舗名")
        parser.add_argument("--admin", help="管理者にする既存ユーザーのユーザー名")

    @transaction.atomic
    def handle(self, *args, **options):
        company, _ = Company.objects.get_or_create(name=options["company"])
        store, _ = Store.objects.get_or_create(company=company, name=options["store"])
        SystemSetting.for_company(company)

        for order, (code, name) in enumerate(CATEGORIES, start=1):
            IngredientCategory.objects.get_or_create(
                company=company, code=code, defaults={"name": name, "sort_order": order}
            )

        items = [(n, AllergenKind.MANDATORY) for n in MANDATORY] + [(n, AllergenKind.RECOMMENDED) for n in RECOMMENDED]
        for order, (name, kind) in enumerate(items, start=1):
            AllergenItem.objects.get_or_create(
                company=company, name=name, defaults={"kind": kind, "sort_order": order}
            )

        if not Supplier.objects.filter(company=company, name="自社").exists():
            Supplier.objects.create(company=company, name="自社", code=IdSequence.issue(company, "SUP"))

        if options["admin"]:
            User = get_user_model()
            try:
                user = User.objects.get(username=options["admin"])
            except User.DoesNotExist:
                raise CommandError(f"ユーザー「{options['admin']}」がいません。先に createsuperuser で作成してください")
            Membership.objects.update_or_create(
                user=user, defaults={"company": company, "store": store, "role": Role.ADMIN}
            )

        self.stdout.write(self.style.SUCCESS(f"初期データを登録しました：{company} / {store}"))
