from django.db import connection

REVENUE_BY_DAY = """
SELECT date_trunc(%s, "shop_order"."created_at") AS day,
       sum("shop_order"."total_cents") AS revenue
FROM "shop_order"
WHERE "shop_order"."created_at" >= %s
GROUP BY 1
ORDER BY 1
"""


def revenue_by_day(since):
    with connection.cursor() as cursor:
        cursor.execute(REVENUE_BY_DAY, ["day", since])
        return cursor.fetchall()
