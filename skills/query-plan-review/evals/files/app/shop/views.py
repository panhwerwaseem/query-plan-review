from django.http import JsonResponse

from .models import Order, Product


def order_history(request, customer_id):
    orders = Order.objects.filter(customer_id=customer_id).order_by("-created_at")[:20]
    payload = []
    for order in orders:
        payload.append({
            "id": order.id,
            "total_cents": order.total_cents,
            "items": [{"sku": item.sku, "quantity": item.quantity} for item in order.items.all()],
        })
    return JsonResponse({"orders": payload})


def orders_by_reference(request):
    ref = request.GET["ref"]
    orders = Order.objects.filter(external_ref=ref).order_by("-created_at")[:20]
    return JsonResponse({"orders": [{"id": o.id, "status": o.status} for o in orders]})


def product_search(request):
    term = request.GET.get("q", "")
    products = Product.objects.filter(name__icontains=term).order_by("name")[:20]
    return JsonResponse({"products": [{"sku": p.sku, "name": p.name} for p in products]})
