"""Generate reproducible demo data for the shop database.

Everything is inserted in one transaction, so a failed run leaves no partial
data. Generation refuses to touch a database that already has orders.
Randomness is seeded so results are stable across runs.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import date, datetime, timedelta

import pymysql

from .config import Settings

DEFAULT_ORDERS = 200_000
DEFAULT_SEED = 42

ProgressFn = Callable[[int], None]

CATEGORIES = [
    "女装", "男装", "童装", "鞋靴", "箱包", "美妆", "护肤", "食品", "饮料", "生鲜",
    "家居", "厨具", "家纺", "数码", "手机配件", "运动户外", "母婴", "宠物", "图书", "文具",
]

CHANNELS = [
    ("自然搜索", "organic"),
    ("付费搜索", "paid"),
    ("社交种草", "social"),
    ("直播带货", "live"),
    ("老客复购", "organic"),
    ("站外联盟", "paid"),
    ("短视频", "social"),
    ("品牌广告", "paid"),
]

PROVINCES = [
    ("广东", "广州", 0.14), ("浙江", "杭州", 0.11), ("江苏", "南京", 0.10),
    ("上海", "上海", 0.08), ("北京", "北京", 0.07), ("山东", "济南", 0.07),
    ("四川", "成都", 0.06), ("河南", "郑州", 0.06), ("湖北", "武汉", 0.05),
    ("福建", "福州", 0.05), ("湖南", "长沙", 0.04), ("河北", "石家庄", 0.04),
    ("安徽", "合肥", 0.04), ("陕西", "西安", 0.03), ("辽宁", "沈阳", 0.03),
    ("江西", "南昌", 0.03),
]

REMOTE_PROVINCES = {"陕西", "辽宁", "四川", "河南", "河北"}

REFUND_REASONS = ["质量问题", "尺码不符", "不喜欢", "物流太慢", "发错货", "七天无理由"]

MONTH_WEIGHT = {
    11: 1.25, 12: 1.55, 1: 1.00, 2: 0.70, 3: 0.90, 4: 0.92,
    5: 0.98, 6: 1.35, 7: 0.88, 8: 0.90, 9: 1.05, 10: 1.08,
}

PROMO_DAYS = {(11, 11): 4.0, (12, 12): 3.2, (6, 18): 3.5}

START_DATE = date(2025, 11, 1)
END_DATE = date(2026, 10, 31)

HIGH_RETURN_PRODUCTS = set(range(1, 11))
LOW_ROI_CHANNEL_ID = 2

BATCH_SIZE = 5000


def weighted_choice(rng: random.Random, items: list[tuple[str, str, float]]) -> tuple[str, str, float]:
    total = sum(item[2] for item in items)
    point = rng.uniform(0, total)
    upto = 0.0
    for item in items:
        upto += item[2]
        if point <= upto:
            return item
    return items[-1]


def random_day(rng: random.Random) -> datetime:
    days = (END_DATE - START_DATE).days
    promises = [(i, MONTH_WEIGHT.get((START_DATE + timedelta(days=i)).month, 1.0))
                for i in range(days + 1)]
    day_index = rng.choices([p[0] for p in promises], weights=[p[1] for p in promises])[0]
    chosen = START_DATE + timedelta(days=day_index)
    if (chosen.month, chosen.day) in PROMO_DAYS:
        seed = rng.random()
        if seed < 0.65:
            offset = rng.randint(-3, 3)
            chosen = chosen + timedelta(days=offset)
    moment = datetime(chosen.year, chosen.month, chosen.day, rng.randint(0, 23),
                      rng.randint(0, 59), rng.randint(0, 59))
    return moment


def seed_base(cur: pymysql.cursors.Cursor, rng: random.Random) -> tuple[int, int]:
    now = datetime(2025, 10, 20, 10, 0, 0)
    cur.executemany(
        "INSERT INTO categories (id, name, parent_id, created_at) VALUES (%s, %s, %s, %s)",
        [(i, name, None, now) for i, name in enumerate(CATEGORIES, start=1)],
    )
    cur.executemany(
        "INSERT INTO channels (id, name, type, created_at) VALUES (%s, %s, %s, %s)",
        [(i, name, kind, now) for i, (name, kind) in enumerate(CHANNELS, start=1)],
    )

    product_rows = []
    for pid in range(1, 301):
        category_id = rng.randint(1, len(CATEGORIES))
        base_price = rng.choice([19.9, 39.9, 59.9, 99.0, 129.0, 199.0, 299.0, 499.0, 899.0])
        price = round(base_price * rng.uniform(0.85, 1.15), 2)
        cost = round(price * rng.uniform(0.40, 0.70), 2)
        launch = date(2024, 1, 1) + timedelta(days=rng.randint(0, 950))
        product_rows.append((
            pid, f"SKU{pid:05d}", f"{CATEGORIES[category_id - 1]}热销款{pid}号",
            category_id, price, cost, launch, 1 if rng.random() > 0.05 else 0,
        ))
    cur.executemany(
        "INSERT INTO products (id, sku, name, category_id, price, cost, launch_date, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        product_rows,
    )

    customer_rows = []
    for cid in range(1, 50001):
        province, city, _ = weighted_choice(rng, PROVINCES)
        registered = datetime(2025, 1, 1) + timedelta(days=rng.randint(0, 640),
                                                      hours=rng.randint(0, 23))
        customer_rows.append((
            cid, f"客户{cid}", f"1{rng.randint(3, 9)}{rng.randint(100000000, 999999999)}",
            province, city, f"{city}市示例路{rng.randint(1, 999)}号", registered,
        ))
    cur.executemany(
        "INSERT INTO customers (id, name, phone, province, city, address, registered_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        customer_rows,
    )
    return len(product_rows), len(customer_rows)


def seed_orders(cur: pymysql.cursors.Cursor, rng: random.Random, product_count: int,
                target_orders: int, on_progress: ProgressFn) -> dict[str, int]:
    stats = {"orders": 0, "items": 0, "refunds": 0, "shipments": 0}
    products = [
        (pid, rng.choice([19.9, 39.9, 59.9, 99.0, 129.0, 199.0, 299.0, 499.0, 899.0]))
        for pid in range(1, product_count + 1)
    ]

    orders: list[tuple] = []
    items: list[tuple] = []
    refunds: list[tuple] = []
    shipments: list[tuple] = []

    item_id = 1
    refund_id = 1
    shipment_id = 1

    def flush() -> None:
        nonlocal orders, items, refunds, shipments
        if orders:
            cur.executemany(
                "INSERT INTO orders (id, order_no, customer_id, channel_id, status, ordered_at, "
                "paid_at, total_amount, shipping_fee, province, city) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", orders,
            )
            cur.executemany(
                "INSERT INTO order_items (id, order_id, product_id, quantity, unit_price, "
                "discount_amount) VALUES (%s, %s, %s, %s, %s, %s)", items,
            )
            if refunds:
                cur.executemany(
                    "INSERT INTO refunds (id, order_id, order_item_id, product_id, reason, "
                    "refund_amount, requested_at, completed_at, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", refunds,
                )
            cur.executemany(
                "INSERT INTO shipments (id, order_id, carrier, tracking_no, shipped_at, "
                "delivered_at, province) VALUES (%s, %s, %s, %s, %s, %s, %s)", shipments,
            )
            stats["orders"] += len(orders)
            stats["items"] += len(items)
            stats["refunds"] += len(refunds)
            stats["shipments"] += len(shipments)
            on_progress(stats["orders"])
        orders, items, refunds, shipments = [], [], [], []

    channel_ids = [i for i in range(1, len(CHANNELS) + 1)]
    channel_weights = [0.24, 0.14, 0.16, 0.12, 0.14, 0.08, 0.07, 0.05]

    for oid in range(1, target_orders + 1):
        ordered_at = random_day(rng)
        channel_id = rng.choices(channel_ids, weights=channel_weights)[0]
        province, city, _ = weighted_choice(rng, PROVINCES)

        roll = rng.random()
        if roll < 0.05:
            status = "cancelled"
        elif roll < 0.20:
            status = "paid"
        elif roll < 0.30:
            status = "shipped"
        elif roll < 0.93:
            status = "completed"
        else:
            status = "refunded"

        paid_at = None if status == "cancelled" else ordered_at + timedelta(minutes=rng.randint(1, 120))
        shipping_fee = 0.0 if rng.random() < 0.7 else rng.choice([8.0, 12.0])

        order_items: list[tuple] = []
        total = 0.0
        for _ in range(rng.randint(1, 3)):
            pid, price = rng.choice(products)
            quantity = 1 if rng.random() < 0.85 else rng.randint(2, 4)
            discount = round(price * quantity * rng.uniform(0, 0.25), 2)
            line_total = round(price * quantity - discount, 2)
            total += line_total
            order_items.append((item_id, oid, pid, quantity, price, discount))
            item_id += 1

        total_amount = round(total + shipping_fee, 2)
        orders.append((oid, f"SO{ordered_at:%Y%m%d}{oid:08d}", rng.randint(1, 50000),
                       channel_id, status, ordered_at, paid_at, total_amount,
                       shipping_fee, province, city))
        items.extend(order_items)

        if status in ("completed", "refunded") and paid_at is not None and order_items:
            for item in order_items:
                product_id = item[2]
                base_rate = 0.30 if product_id in HIGH_RETURN_PRODUCTS else 0.06
                if rng.random() < base_rate:
                    amount = round(item[4] * item[3] - item[5], 2)
                    requested = paid_at + timedelta(days=rng.randint(1, 20))
                    completed = rng.random() < 0.9
                    refunds.append((
                        refund_id, oid, item[0], product_id, rng.choice(REFUND_REASONS),
                        amount, requested,
                        requested + timedelta(days=rng.randint(1, 7)) if completed else None,
                        "completed" if completed else "requested",
                    ))
                    refund_id += 1

        if status != "cancelled" and paid_at is not None:
            shipped_at = paid_at + timedelta(days=rng.randint(1, 2), hours=rng.randint(0, 23))
            travel = rng.randint(1, 10) + (4 if province in REMOTE_PROVINCES else 0)
            delivered = shipped_at + timedelta(days=travel) if status != "shipped" else None
            shipments.append((shipment_id, oid, rng.choice(["顺丰", "中通", "圆通", "韵达", "京东物流"]),
                              f"SF{rng.randint(100000000, 999999999)}", shipped_at, delivered, province))
            shipment_id += 1

        if len(orders) >= BATCH_SIZE:
            flush()

    flush()
    return stats


def seed_ads(cur: pymysql.cursors.Cursor, rng: random.Random, product_count: int) -> dict[str, int]:
    campaigns = []
    for cid in range(1, 61):
        channel_id = rng.choice([2, 3, 4, 6, 8])
        start = START_DATE + timedelta(days=rng.randint(0, 240))
        end = min(start + timedelta(days=rng.randint(30, 180)), END_DATE)
        campaigns.append((cid, f"活动{cid}号-{CHANNELS[channel_id - 1][0]}",
                          channel_id, rng.randint(1, product_count),
                          round(rng.uniform(200, 5000), 2), start, end))
    cur.executemany(
        "INSERT INTO ad_campaigns (id, campaign_name, channel_id, product_id, daily_budget, "
        "start_date, end_date) VALUES (%s, %s, %s, %s, %s, %s, %s)", campaigns,
    )

    roi_factor = {2: 1.15, 3: 2.60, 4: 3.10, 6: 1.80, 8: 1.45}
    ctr = {2: 0.012, 3: 0.035, 4: 0.055, 6: 0.020, 8: 0.015}

    rows: list[tuple] = []
    stat_id = 1
    for cid, _, channel_id, _, budget, start, end in campaigns:
        day = start
        while day <= end:
            spend = round(budget * rng.uniform(0.75, 1.0), 2)
            impressions = int(spend / rng.uniform(0.02, 0.06))
            clicks = max(1, int(impressions * ctr[channel_id] * rng.uniform(0.8, 1.2)))
            factor = roi_factor[channel_id] * rng.uniform(0.7, 1.3)
            attributed_gmv = round(spend * factor, 2)
            rows.append((stat_id, cid, day, impressions, clicks, spend, attributed_gmv,
                         max(1, int(attributed_gmv / rng.uniform(80, 200)))))
            stat_id += 1
            day += timedelta(days=1)
            if len(rows) >= BATCH_SIZE:
                cur.executemany(
                    "INSERT INTO ad_daily_stats (id, campaign_id, stat_date, impressions, clicks, "
                    "spend, attributed_gmv, attributed_orders) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", rows,
                )
                rows = []
    if rows:
        cur.executemany(
            "INSERT INTO ad_daily_stats (id, campaign_id, stat_date, impressions, clicks, "
            "spend, attributed_gmv, attributed_orders) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", rows,
        )
    return {"campaigns": len(campaigns), "ad_stats": stat_id - 1}


def has_orders(settings: Settings) -> bool:
    connection = _connect(settings)
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT EXISTS(SELECT 1 FROM orders)")
            return bool(cur.fetchone()[0])
    finally:
        connection.close()


def _connect(settings: Settings) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_admin_user,
        password=settings.mysql_admin_password,
        database="shop",
        charset="utf8mb4",
        autocommit=False,
    )


def generate(settings: Settings, on_progress: ProgressFn, orders: int = DEFAULT_ORDERS,
             seed: int = DEFAULT_SEED) -> None:
    if has_orders(settings):
        raise RuntimeError("业务库已有订单，不生成模拟数据")

    rng = random.Random(seed)
    connection = _connect(settings)
    try:
        with connection.cursor() as cur:
            product_count, _ = seed_base(cur, rng)
            seed_orders(cur, rng, product_count, orders, on_progress)
            seed_ads(cur, rng, product_count)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
