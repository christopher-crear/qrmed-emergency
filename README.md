# QRMed Emergency — Panel administrador y paciente con Django + Supabase

Aplicación de dos roles construida con Django 6, PostgreSQL/Supabase, Supabase Auth y una interfaz responsive basada en los diseños entregados.

## Funciones incluidas

- Inicio con estadísticas reales, gráficos mensuales, pacientes y actividad reciente.
- Pacientes: búsqueda, ficha médica y edición en tres pasos.
- Listado de pacientes fiel al diseño: tabla, filtros, exportación, estados y acciones.
- Códigos QR reales descargables con ficha pública de emergencia.
- Pagos: métricas, búsqueda automática, filtros, comprobantes privados, aprobación/rechazo y exportación CSV.
- Productos: catálogo rediseñado, búsqueda automática e imágenes tomadas exclusivamente de `products.image_url`.
- Pedidos: seguimiento visual, detalle, estado, entrega y rastreo.
- Carrito: permite selecciones repetidas e independientes, conserva todas las líneas al aplicar descuentos y muestra los cupones disponibles en un desplegable.
- Pago idempotente: cada confirmación genera como máximo un pedido, incluso ante doble clic, reintento del navegador o respuesta lenta del servidor.
- Detalle de compra: lista completa de productos, variantes, cantidades, subtotal, descuento, total, código de entrega, comprobante y factura.
- Flujo de pago y entrega: un pago aprobado pasa a producción con fecha estimada a siete días; un rechazo cancela el pedido y notifica el motivo al cliente.
- Entrega segura: el código permanece visible para administración/motorizado y el cliente lo ingresa desde “Mis pedidos” para marcar la manilla como entregada.
- Usuarios: panel de métricas, búsqueda automática, fotografías reales, roles y bloqueo mediante `profiles.role` y `profiles.is_active`.
- Perfil: portada y avatar privados, edición integrada, actividad y cambio de contraseña validando primero la contraseña actual.
- Configuración: idioma, tema, notificaciones y privacidad.
- Bancos y cuentas: módulo administrativo multibanco con logo, QR, orden, visibilidad y edición; las cuentas visibles aparecen automáticamente en el checkout del paciente.
- Iconos incluidos localmente para que el menú funcione sin depender de un CDN.
- Configuración lista para Render mediante `render.yaml` y `build.sh`.
- Login compartido con redirección automática según `profiles.role`.
- Panel del paciente con dashboard, credencial QR reversible, tienda, carrito y checkout en tres pasos (envío, pago y confirmación), comprobante real, pantalla de pedido recibido, ficha médica, pedidos, perfil y preferencias.
- Cada paciente solo consulta su propia ficha y los pedidos vinculados a su UUID de Auth.

## 1. Probar localmente en Windows con Supabase real

Abre PowerShell dentro de esta carpeta y ejecuta:

```powershell
py -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py runserver
```

Antes de ejecutar, edita `.env` con tus valores reales:

```env
DEMO_MODE=False
DATABASE_URL=postgresql://postgres.PROJECT_REF:TU_PASSWORD@HOST-POOLER:6543/postgres
DATABASE_CONN_MAX_AGE=0
SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...
```

Obtén `DATABASE_URL` en Supabase: **Project → Connect → Transaction pooler**. Para la aplicación Django usa el puerto `6543`; es compatible con IPv4 y permite reutilizar mejor las conexiones. El proyecto desactiva automáticamente consultas preparadas y cursores de servidor al detectar ese puerto. `DATABASE_CONN_MAX_AGE=0` evita que cada proceso de Django reserve una conexión de forma persistente.

Para ejecutar migraciones puntuales también puedes usar temporalmente el **Session pooler** (`5432`), siempre que existan conexiones disponibles. Al terminar, vuelve al puerto `6543` para ejecutar la aplicación.

### Error `EMAXCONNSESSION: max clients reached`

Ese mensaje significa que el pooler de sesión alcanzó su límite; no es un error de contraseña. Haz lo siguiente:

1. Detén todos los `runserver`, consolas, clientes SQL y servicios antiguos que usen la base.
2. En Supabase abre **Project → Connect**, copia la cadena de **Transaction pooler** y reemplaza el puerto `5432` por la cadena exacta que Supabase muestra con puerto `6543`.
3. Mantén `DATABASE_CONN_MAX_AGE=0` en `.env`.
4. Espera unos minutos para que Supabase cierre conexiones inactivas y ejecuta `python manage.py runserver` de nuevo.
5. Si sigue lleno, revisa **Database → Settings → Connection pooling** y los clientes activos antes de aumentar el pool size.

No abras varias instancias de `runserver` al mismo tiempo. El autoreloader de Django no requiere cambiar `CONN_MAX_AGE` y el proyecto ya cierra cada conexión al terminar la solicitud.

Las claves están en **Project Settings → API**. La `service_role` se usa únicamente en el servidor para subir archivos; nunca debe colocarse en HTML, JavaScript, GitHub ni una aplicación móvil.

Las tablas del negocio son modelos `managed = False`, por lo que `migrate` solo crea las tablas internas de Django para sesiones; no recrea tus tablas `profiles`, `patients`, `products`, etc. Si `DATABASE_URL` existe, el proyecto fuerza el uso de Supabase aunque quedara una variable antigua `DEMO_MODE=True` en Render.

### Buckets de Storage

Los nombres predeterminados son:

- `patient-photos`: fotos de pacientes.
- `payment-proofs`: comprobantes de pago.
- `profile-images`: avatar y portada.
- `bank-assets`: logos y códigos QR de las cuentas bancarias.

### Preparar el módulo Bancos y cuentas

Antes de usar el nuevo módulo en Supabase, abre **SQL Editor**, pega el contenido de `supabase_bancos.sql` y ejecútalo. El script crea la tabla `bank_accounts`, el bucket privado `bank-assets` y sus políticas RLS. Después agrega en Render la variable `SUPABASE_BANK_BUCKET=bank-assets` (el proyecto ya usa ese valor por defecto).

La configuración bancaria antigua de `payment_settings` se conserva únicamente como respaldo: si todavía no existe ninguna cuenta visible en `bank_accounts`, el checkout sigue mostrando la cuenta anterior para no interrumpir los pagos.

Si tus buckets tienen otros nombres, cambia las variables `SUPABASE_*_BUCKET`. Por compatibilidad, los valores antiguos `patient-files` y `profiles` se corrigen automáticamente a los nombres reales anteriores. El panel normaliza rutas con o sin el prefijo del bucket. Las fotos de pacientes y perfiles se entregan mediante endpoints de Django que generan una redirección temporal firmada hacia el objeto privado exacto; como respaldo, el servidor también puede descargar el archivo con la clave de servicio. La ruta se puede recuperar por UUID aunque `avatar_path` o `photo_path` esté vacío. En `profile-images` la recuperación distingue estrictamente archivos `avatar-*` de `cover-*`, evitando mostrar una portada como foto de usuario. Los comprobantes usan URLs temporales firmadas. Las imágenes de productos no usan Storage: se muestran únicamente desde la URL guardada en `products.image_url`. Los archivos subidos se rebobinan antes de enviarlos a Storage y las URLs privadas del perfil incorporan una versión basada en `updated_at`, para que al cambiar avatar o portada el navegador muestre el archivo nuevo inmediatamente.

Cuando un perfil o paciente no tiene fotografía, o la imagen privada deja de estar disponible, el panel muestra automáticamente las dos primeras iniciales de su nombre. Nunca reutiliza la foto de otra persona.

### Rendimiento y consumo de recursos

El proyecto está preparado para planes pequeños de Supabase y Render:

- usa el Transaction pooler (`6543`) con `DATABASE_CONN_MAX_AGE=0` para no agotar las conexiones;
- reutiliza en cada request el perfil y paciente que ya validó el decorador, evitando consultas duplicadas;
- guarda sesiones en `cached_db` y limita la caché local a 1000 entradas;
- conserva temporalmente URLs firmadas, imágenes privadas y códigos QR;
- el navegador mantiene las redirecciones de imágenes durante 15 minutos;
- el panel del paciente calcula los estados de pedidos en una sola agregación y no consulta productos solo para contar el carrito;
- no depende de Google Fonts: utiliza la tipografía del sistema y elimina una petición externa por página.

La caché es local y acotada, por lo que no requiere Redis ni otro servicio adicional.

Para comprobar por separado la conexión PostgreSQL y la autorización de Storage sin imprimir las claves, ejecuta:

```powershell
python manage.py diagnose_storage
```

El comando verifica la ruta de un avatar real, genera una URL firmada y confirma su descarga. Si la clave de servicio falla, las vistas administrativas intentan además firmar la imagen con el token del administrador que inició sesión.

## 2. Roles válidos

El usuario debe existir en Supabase Auth y su UUID debe coincidir con `profiles.id`. Además:

```sql
update public.profiles
set role = 'administrador', is_active = true
where id = 'UUID-DEL-USUARIO-DE-AUTH';
```

Para una cuenta de paciente, su UUID de Auth debe coincidir con `profiles.id` y estar vinculado mediante `patients.owner_id`:

```sql
update public.profiles
set role = 'usuario', is_active = true
where id = 'UUID-DEL-PACIENTE-EN-AUTH';

update public.patients
set owner_id = 'UUID-DEL-PACIENTE-EN-AUTH'
where id = 'UUID-DE-LA-FICHA';
```

Se admiten los pares `admin`/`administrador` y `user`/`usuario` (también `patient`/`paciente`). Las cuentas inactivas se rechazan.

## 3. Desplegar en Render

### Opción A — servicio existente

1. Sube esta carpeta a GitHub.
2. En tu servicio Render, selecciona el repositorio y la rama.
3. Configura **Build Command**: `./build.sh`.
4. Configura **Start Command**: `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --timeout 120`.
5. Configura **Health Check Path**: `/health/`.
6. Agrega todas las variables de producción listadas abajo.
7. Ejecuta **Manual Deploy → Clear build cache & deploy** si el servicio anterior usaba otro lenguaje.

### Opción B — Blueprint

En Render selecciona **New → Blueprint**, conecta el repositorio y usa el `render.yaml` incluido. Render solicitará las variables marcadas como `sync: false`.

Variables requeridas en producción:

| Variable | Ejemplo |
|---|---|
| `DEBUG` | `False` |
| `SECRET_KEY` | Una cadena larga aleatoria |
| `ALLOWED_HOSTS` | `tu-servicio.onrender.com` |
| `CSRF_TRUSTED_ORIGINS` | `https://tu-servicio.onrender.com` |
| `DEMO_MODE` | `False` |
| `DATABASE_URL` | Cadena del Transaction pooler de Supabase (`6543`) |
| `DATABASE_CONN_MAX_AGE` | Segundos de persistencia de conexión; usa `0` con Supabase |
| `SUPABASE_URL` | `https://PROJECT_REF.supabase.co` |
| `SUPABASE_ANON_KEY` | Clave anon de Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | Clave secreta solo del servidor |
| `QRMED_COMPANY_NAME` | Nombre que aparecerá en las facturas |
| `QRMED_COMPANY_TAX_ID` | RUC o identificación de la empresa |
| `QRMED_COMPANY_ADDRESS` | Dirección comercial |
| `QRMED_COMPANY_PHONE` | Teléfono comercial |
| `QRMED_COMPANY_EMAIL` | Correo que aparecerá en las facturas |

Después del despliegue revisa `/health/`. Debe responder `{"status":"ok"}`.

## 4. Pruebas y comprobaciones

```powershell
python manage.py check
python manage.py test
python manage.py check --deploy
```

## Estructura principal

```text
config/                 Configuración de Django
panel/models.py         Mapeo de tablas existentes de Supabase
panel/services.py       Supabase Auth y Storage
panel/views.py          Lógica del panel administrativo
panel/patient_views.py  Lógica aislada del panel de paciente
panel/templates/panel/  Vistas HTML
panel/static/panel/     CSS, JavaScript e iconos
render.yaml             Infraestructura de Render
build.sh                Instalación, estáticos y migraciones
```

## 5. Activar descuentos, notificaciones, facturas y reactivaciones en Supabase

Abre **Supabase → SQL Editor**, copia todo el contenido de `supabase_actualizacion_completa.sql` y ejecútalo. El script es idempotente y crea las tablas `discount_campaigns`, `discount_tickets`, `notification_reads`, `invoices` y `activation_requests`, junto con sus índices, restricciones y políticas RLS.

Este script no crea una clave foránea hacia `public.orders`, por lo que también funciona en proyectos donde la tabla de pedidos usa otro nombre. Esto corrige el error `relation "public.orders" does not exist`.

La aplicación administra campañas desde `/descuentos/`; los pacientes reclaman tickets en `/mi/descuentos/`. Las facturas aprobadas se envían al buzón del cliente desde la validación de pagos. Las solicitudes de reactivación llegan a `/buzon/` y la eliminación confirmada por contraseña borra la identidad mediante Supabase Auth Admin. Nunca expongas `SUPABASE_SERVICE_ROLE_KEY` en el navegador: debe existir únicamente como variable secreta de Render.

### Actualización del 23 de agosto de 2026

Antes de desplegar esta versión ejecuta también `supabase_mejoras_qrmed_20260823.sql`. El archivo agrega el stock mínimo a productos y guarda el banco utilizado en cada pedido sin eliminar registros existentes.

La recuperación de contraseña usa el correo seguro de Supabase Auth. En **Authentication → URL Configuration** agrega como URL permitida `https://TU-SERVICIO.onrender.com/recuperar-contrasena/nueva/`. Las contraseñas actuales nunca se muestran ni se envían por WhatsApp.

El botón **Enviar por WhatsApp** de la factura utiliza el menú de compartir del dispositivo para adjuntar el PDF. En navegadores que no soportan archivos compartidos abre WhatsApp con el texto listo; por seguridad del navegador, el administrador confirma el envío final.

## Referencias oficiales

- Django — lista de verificación de despliegue: https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/
- Render — desplegar una aplicación Django: https://render.com/docs/deploy-django
- Render — configuración de Blueprints: https://render.com/docs/infrastructure-as-code
- Supabase — conexiones directas y poolers: https://supabase.com/docs/guides/database/connecting-to-postgres
