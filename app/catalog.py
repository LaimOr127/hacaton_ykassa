from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    title: str
    unit_kopecks: int
    basis: str = "unit"
    tiers: tuple[tuple[int, int, int], ...] = ()

    def item(self, sku: str, value: int) -> dict:
        title, quantity, price = self.title, value, self.unit_kopecks
        if self.basis == "fixed" and value != 1:
            raise ValueError(f"Позицию «{title}» можно добавить один раз.")
        if self.basis == "area":
            price = next((amount for lower, upper, amount in self.tiers if lower <= value <= upper), None)
            if price is None:
                raise ValueError(f"В прайсе нет тарифа для площади {value} м².")
            title, quantity = f"{title} · {value} м²", 1
        return {
            "sku": sku, "title": title, "quantity": quantity,
            "unit_kopecks": price, "amount_kopecks": quantity * price,
        }


@dataclass(frozen=True)
class Catalog:
    merchant_id: str
    title: str
    products: dict[str, Product]
    deposit_percent: int = 100

    def price(self, quantities: dict[str, int]) -> dict:
        items = [self.products[sku].item(sku, value) for sku, value in quantities.items()]
        total = sum(item["amount_kopecks"] for item in items)
        payable = (total * self.deposit_percent + 50) // 100
        return {
            "items": items,
            "total_kopecks": total,
            "payable_kopecks": payable,
            "balance_kopecks": total - payable,
            "currency": "RUB",
        }


COFFEE = Catalog(
    merchant_id="coffee",
    title="Кофейня «Утро»",
    products={
        "breakfast": Product("Обычный завтрак", 35_000),
        "vegetarian": Product("Вегетарианский завтрак", 40_000),
    },
)
