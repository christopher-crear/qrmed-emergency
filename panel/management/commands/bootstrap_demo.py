import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from panel.models import (
    BankAccount, DiscountCampaign, DiscountTicket, Invoice, NotificationRead,
    Order, OrderItem, Patient, PaymentSetting, Product, Profile,
)


class Command(BaseCommand):
    help = "Crea las tablas existentes y datos de ejemplo únicamente en SQLite."

    def handle(self, *args, **options):
        if connection.vendor != "sqlite":
            raise CommandError("bootstrap_demo solo puede ejecutarse con SQLite y nunca modifica Supabase.")
        self._create_tables()
        self._seed()
        self.stdout.write(self.style.SUCCESS("Demo creada. Ingresa con admin@qrmed.ec / admin123"))

    def _create_tables(self):
        statements = [
            """CREATE TABLE IF NOT EXISTS profiles (id varchar(36) PRIMARY KEY, full_name text, phone text, role varchar(40), is_active bool, created_at datetime, updated_at datetime, city text, specialty text, avatar_path text, cover_path text, preferences text)""",
            """CREATE TABLE IF NOT EXISTS patients (id varchar(36) PRIMARY KEY, owner_id varchar(36), first_name text, last_name text, id_number text, birth_date date, sex text, address text, city text, phone text, email text, blood_type text, insurance text, allergies text, diseases text, medications text, disabilities text, history text, notes text, emergency_name text, emergency_relationship text, emergency_phone text, qr_token varchar(36), status varchar(30), created_at datetime, updated_at datetime, photo_path text, emergency2_name text, emergency2_relationship text, emergency2_phone text, qr_scan_count bigint, last_qr_scan_at datetime)""",
            """CREATE TABLE IF NOT EXISTS medical_documents (id varchar(36) PRIMARY KEY, patient_id varchar(36), owner_id varchar(36), document_type text, original_name text, storage_path text, mime_type text, size_bytes bigint, created_at datetime)""",
            """CREATE TABLE IF NOT EXISTS products (id varchar(36) PRIMARY KEY, name text, description text, price decimal(12,2), stock integer, image_url text, is_active bool, created_at datetime, updated_at datetime, colors text, sizes text, badge text)""",
            """CREATE TABLE IF NOT EXISTS orders (id varchar(36) PRIMARY KEY, user_id varchar(36), order_number text UNIQUE, total decimal(12,2), status varchar(40), payment_method varchar(40), payment_proof_path text, shipping_address text, created_at datetime, updated_at datetime, payment_rejection_reason text, payment_reviewed_at datetime, payment_reviewed_by varchar(36), tracking_number text, estimated_delivery date, subtotal decimal(12,2), discount_amount decimal(12,2), discount_code text, shipping_name text, shipping_city text, shipping_postal text, shipping_phone text)""",
            """CREATE TABLE IF NOT EXISTS order_items (id varchar(36) PRIMARY KEY, order_id varchar(36), product_id varchar(36), quantity integer, unit_price decimal(12,2), selected_color text, selected_size text)""",
            """CREATE TABLE IF NOT EXISTS payment_settings (id bool PRIMARY KEY, bank_name text, account_type text, account_number text, interbank_code text, account_holder text, tax_id text, notification_email text, transfer_qr_payload text, updated_at datetime)""",
            """CREATE TABLE IF NOT EXISTS bank_accounts (id varchar(36) PRIMARY KEY, bank_name text, account_holder text, account_number text, account_type text, tax_id text, instructions text, is_visible bool, display_order integer, logo_path text, qr_path text, created_by varchar(36), created_at datetime, updated_at datetime)""",
            """CREATE TABLE IF NOT EXISTS discount_campaigns (id varchar(36) PRIMARY KEY, code varchar(40) UNIQUE, title varchar(120), description text, discount_type varchar(20), discount_value decimal(12,2), min_order_amount decimal(12,2), max_claims integer, starts_at datetime, expires_at datetime, is_active bool, created_by varchar(36), created_at datetime, updated_at datetime)""",
            """CREATE TABLE IF NOT EXISTS discount_tickets (id varchar(36) PRIMARY KEY, campaign_id varchar(36), user_id varchar(36), claimed_at datetime, used_at datetime, order_id varchar(36), UNIQUE(campaign_id, user_id))""",
            """CREATE TABLE IF NOT EXISTS notification_reads (id varchar(36) PRIMARY KEY, user_id varchar(36), notification_key varchar(180), read_at datetime, UNIQUE(user_id, notification_key))""",
            """CREATE TABLE IF NOT EXISTS invoices (id varchar(36) PRIMARY KEY, order_id varchar(36) UNIQUE, user_id varchar(36), invoice_number varchar(50) UNIQUE, issued_at datetime, sent_at datetime, created_by varchar(36))""",
        ]
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    def _seed(self):
        now = timezone.now()
        admin_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
        user_ids = [uuid.UUID(f"22222222-2222-2222-2222-{i:012d}") for i in range(1, 6)]
        Profile.objects.update_or_create(id=admin_id, defaults={"full_name": "Christopher Eras", "phone": "0967332746", "role": "administrador", "is_active": True, "created_at": now - timedelta(days=20), "updated_at": now, "city": "Loja", "specialty": "Administrador", "preferences": {"theme": "light", "language": "es", "order_updates": True}})
        names = [("Cristian Gonzaga", "0969124476"), ("Christopher Granda", "09989414258"), ("Rafael Alexander Eras Granda", "0967907835"), ("Walter Orion", "0989144258"), ("Ana Lucía Torres", "0981122334")]
        for i, (name, phone) in enumerate(names):
            Profile.objects.update_or_create(id=user_ids[i], defaults={"full_name": name, "phone": phone, "role": "usuario", "is_active": i != 4, "created_at": now - timedelta(days=35 - i * 5), "updated_at": now, "city": "Loja", "preferences": {}})
        patient_ids = [uuid.UUID(f"33333333-3333-3333-3333-{i:012d}") for i in range(1, 6)]
        for i, (name, phone) in enumerate(names):
            parts = name.split()
            Patient.objects.update_or_create(id=patient_ids[i], defaults={"owner_id": user_ids[i], "first_name": parts[0], "last_name": " ".join(parts[1:]), "id_number": f"1105{i+1}011{i}", "birth_date": timezone.localdate() - timedelta(days=8000 + i * 700), "sex": "male" if i < 4 else "female", "address": f"Barrio Central, calle {i+1}", "city": "Loja", "phone": phone, "email": f"paciente{i+1}@example.com", "blood_type": ["O+", "A+", "B-", "O-", "AB+"][i], "insurance": "IESS" if i % 2 == 0 else "", "allergies": ["Penicilina"] if i == 0 else [], "diseases": ["Hipertensión"] if i == 2 else [], "medications": ["Losartán 50 mg"] if i == 2 else [], "disabilities": "", "history": "Sin antecedentes relevantes", "notes": "", "emergency_name": "María Familiar", "emergency_relationship": "Madre", "emergency_phone": "0990001122", "qr_token": uuid.uuid4(), "status": "inactive" if i == 4 else "active", "created_at": now - timedelta(days=28 - i * 5), "updated_at": now, "qr_scan_count": i * 2})
        product_ids = [uuid.UUID("44444444-4444-4444-4444-444444444441"), uuid.UUID("44444444-4444-4444-4444-444444444442")]
        Product.objects.update_or_create(id=product_ids[0], defaults={"name": "Pulsera de acero inoxidable", "description": "Pulsera resistente con código QR médico grabado.", "price": Decimal("15.00"), "stock": 7, "image_url": "https://images.unsplash.com/photo-1617038220319-276d3cfab638?w=900", "is_active": True, "created_at": now - timedelta(days=30), "updated_at": now, "colors": ["Plateado", "Negro"], "sizes": ["S", "M", "L"], "badge": "Más popular"})
        Product.objects.update_or_create(id=product_ids[1], defaults={"name": "Manillas QR", "description": "Manilla ligera disponible en varios colores.", "price": Decimal("10.00"), "stock": 18, "image_url": "https://images.unsplash.com/photo-1602173574767-37ac01994b2a?w=900", "is_active": True, "created_at": now - timedelta(days=20), "updated_at": now, "colors": ["Azul", "Rojo", "Negro"], "sizes": ["S", "M", "L"], "badge": ""})
        states = ["pending", "pending", "production", "shipped", "delivered"]
        totals = [Decimal("10.00"), Decimal("30.00"), Decimal("25.00"), Decimal("18.50"), Decimal("29.99")]
        for i in range(5):
            oid = uuid.UUID(f"55555555-5555-5555-5555-{i+1:012d}")
            reviewed = now - timedelta(days=6-i) if i >= 2 else None
            order, _ = Order.objects.update_or_create(id=oid, defaults={"user_id": user_ids[i % len(user_ids)], "order_number": f"ORD-202608{11-i:02d}-{['4C5464','34EDC3','252299','FF05A5','93AE95'][i]}", "total": totals[i], "status": states[i], "payment_method": "transferencia" if i % 2 == 0 else "deposito", "payment_proof_path": "https://images.unsplash.com/photo-1554224155-8d04cb21cd6c?w=900", "shipping_address": "Loja, Ecuador", "created_at": now - timedelta(days=i+2), "updated_at": now, "payment_reviewed_at": reviewed, "payment_reviewed_by": admin_id if reviewed else None, "estimated_delivery": timezone.localdate() + timedelta(days=7-i), "subtotal": totals[i], "discount_amount": 0, "shipping_name": names[i][0], "shipping_city": "Loja", "shipping_phone": names[i][1]})
            item_id = uuid.UUID(f"66666666-6666-6666-6666-{i+1:012d}")
            OrderItem.objects.update_or_create(id=item_id, defaults={"order_id": order.id, "product_id": product_ids[i % 2], "quantity": 1 if i != 1 else 2, "unit_price": totals[i] if i != 1 else Decimal("15.00"), "selected_color": "Azul" if i % 2 else "Plateado", "selected_size": "M"})
        PaymentSetting.objects.update_or_create(id=True, defaults={"bank_name": "Banco de Loja", "account_type": "Ahorros", "account_number": "1106056011", "interbank_code": "", "account_holder": "QR Med", "tax_id": "1105601105", "notification_email": "qrmedicsupport@gmail.com", "transfer_qr_payload": '{"banco":"Banco de Loja","cuenta":"1106056011"}', "updated_at": now})
        demo_banks = [
            ("77777777-7777-7777-7777-777777777771", "Banco de Loja", "29045612", 0),
            ("77777777-7777-7777-7777-777777777772", "Banco Pichincha", "89456235", 1),
            ("77777777-7777-7777-7777-777777777773", "CoopMego", "56789123", 2),
            ("77777777-7777-7777-7777-777777777774", "Cooperativa JEP", "65489256", 3),
        ]
        for bank_id, bank_name, account_number, order in demo_banks:
            BankAccount.objects.update_or_create(
                id=uuid.UUID(bank_id),
                defaults={
                    "bank_name": bank_name,
                    "account_holder": "QRMed Emergency",
                    "account_number": account_number,
                    "account_type": "Ahorros",
                    "tax_id": "1106056011",
                    "instructions": "Incluye tu nombre y número de pedido en el concepto.",
                    "is_visible": True,
                    "display_order": order,
                    "created_by": admin_id,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        DiscountCampaign.objects.update_or_create(
            id=uuid.UUID("88888888-8888-8888-8888-888888888881"),
            defaults={
                "code": "QRMED-BIENVENIDA", "title": "Bienvenida QRMed",
                "description": "Obtén un descuento especial en tu próxima pulsera.",
                "discount_type": "percentage", "discount_value": Decimal("15.00"),
                "min_order_amount": Decimal("10.00"), "max_claims": 25,
                "starts_at": now - timedelta(days=1), "expires_at": now + timedelta(days=30),
                "is_active": True, "created_by": admin_id, "created_at": now, "updated_at": now,
            },
        )
