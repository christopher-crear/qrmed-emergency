-- QRMed Emergency - carrito, pagos y stock mínimo
-- Ejecutar una sola vez en Supabase > SQL Editor antes de desplegar esta versión.
-- El script es idempotente y conserva todos los datos existentes.

do $$
begin
  if to_regclass('public.products') is null then
    raise exception 'No existe la tabla public.products';
  end if;
  if to_regclass('public.orders') is null then
    raise exception 'No existe la tabla public.orders';
  end if;

  execute 'alter table public.products add column if not exists min_stock integer not null default 5';
  execute 'alter table public.orders add column if not exists payment_bank_id uuid';
  execute 'alter table public.orders add column if not exists payment_bank_name text';

  if not exists (
       select 1 from pg_constraint
       where conname = 'products_min_stock_nonnegative'
         and conrelid = 'public.products'::regclass
     ) then
    execute 'alter table public.products add constraint products_min_stock_nonnegative check (min_stock >= 0)';
  end if;

  execute 'create index if not exists products_stock_alert_idx on public.products (is_active, stock, min_stock)';
  execute 'create index if not exists orders_payment_bank_idx on public.orders (payment_bank_id) where payment_bank_id is not null';

  execute $sql$comment on column public.products.min_stock is
    'Umbral para mostrar una alerta administrativa de inventario bajo.'$sql$;
  execute $sql$comment on column public.orders.payment_bank_id is
    'Cuenta bancaria seleccionada por el cliente al registrar el pago.'$sql$;
  execute $sql$comment on column public.orders.payment_bank_name is
    'Nombre histórico del banco utilizado, conservado aunque la cuenta cambie.'$sql$;
end $$;

-- Verificación final: deben aparecer las tres columnas.
select table_name, column_name, data_type
from information_schema.columns
where table_schema = 'public'
  and (
    (table_name = 'products' and column_name = 'min_stock')
    or
    (table_name = 'orders' and column_name in ('payment_bank_id', 'payment_bank_name'))
  )
order by table_name, column_name;
