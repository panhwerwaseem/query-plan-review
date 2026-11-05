from django.db import models


class Customer(models.Model):
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=120)


class Product(models.Model):
    sku = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=200)


class Order(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="orders")
    external_ref = models.CharField(max_length=32)
    status = models.CharField(max_length=16, default="new")
    total_cents = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    sku = models.CharField(max_length=40)
    quantity = models.IntegerField()
    unit_price_cents = models.IntegerField()
