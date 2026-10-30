# Vigilante de errores de precio

Revisa tiendas de tecnología, hogar y ferretería cada 10 minutos y avisa por Telegram cuando un producto aparece muchísimo más barato de lo normal.

## Cómo decide que es un error

- **Caída brusca:** el precio bajó 60% o más comparado con lo que venía costando.
- **Precio imposible:** un producto "caro" (notebook, heladera, iPhone, taladro, etc.) con un precio por debajo del mínimo lógico. Accesorios como fundas o cables no cuentan.
- Cada producto avisa una sola vez, hasta que vuelva a su precio normal.
- Si una tienda entera baja de golpe (cambio de moneda, liquidación), manda un solo aviso general en lugar de cientos.

## Ajustes (archivo config.json)

- `dolar_en_pesos`: para comparar precios en USD y en pesos.
- `caida_minima`: 0.6 = avisa con caídas del 60% o más.
- `minimos_por_palabra`: precio mínimo lógico por tipo de producto, en pesos.
- `tiendas`: para agregar una tienda, copiar una línea y cambiar nombre y url. Para pausarla, agregarle `"activa": false`.
