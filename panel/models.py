import json
import uuid

from django.db import connection, models


class TextArrayField(models.Field):
    """Campo compatible con text[] de PostgreSQL y con SQLite para el modo demo."""

    description = "Lista de textos"

    def db_type(self, connection):
        return "text[]" if connection.vendor == "postgresql" else "text"

    def from_db_value(self, value, expression, connection):
        if value is None or isinstance(value, list):
            return value or []
        if isinstance(value, tuple):
            return list(value)
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return [x.strip() for x in str(value).strip("{}").split(",") if x.strip()]

    def to_python(self, value):
        return self.from_db_value(value, None, connection)

    def get_prep_value(self, value):
        if value in (None, ""):
            return [] if connection.vendor == "postgresql" else "[]"
        if isinstance(value, str):
            value = [x.strip() for x in value.split(",") if x.strip()]
        return value if connection.vendor == "postgresql" else json.dumps(value)


class ExistingTable(models.Model):
    class Meta:
        abstract = True
        managed = False


class Profile(ExistingTable):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    full_name = models.TextField(blank=True, null=True)
    phone = models.TextField(blank=True, null=True)
    role = models.CharField(max_length=40, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)
    city = models.TextField(blank=True, null=True)
    specialty = models.TextField(blank=True, null=True)
    avatar_path = models.TextField(blank=True, null=True)
    cover_path = models.TextField(blank=True, null=True)
    preferences = models.JSONField(default=dict, blank=True)

    class Meta(ExistingTable.Meta):
        db_table = "profiles"


class Patient(ExistingTable):
    STATUS_CHOICES = [("active", "Activo"), ("inactive", "Inactivo")]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    owner_id = models.UUIDField()
    first_name = models.TextField()
    last_name = models.TextField()
    id_number = models.TextField()
    birth_date = models.DateField(blank=True, null=True)
    # Los valores admitidos pertenecen al CHECK de la base existente. No se
    # duplican como ``choices`` porque pueden variar entre proyectos Supabase.
    sex = models.TextField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    city = models.TextField(blank=True, null=True)
    phone = models.TextField(blank=True, null=True)
    email = models.TextField(blank=True, null=True)
    blood_type = models.TextField(blank=True, null=True)
    insurance = models.TextField(blank=True, null=True)
    allergies = TextArrayField(blank=True, default=list)
    diseases = TextArrayField(blank=True, default=list)
    medications = TextArrayField(blank=True, default=list)
    disabilities = models.TextField(blank=True, null=True)
    history = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    emergency_name = models.TextField(blank=True, null=True)
    emergency_relationship = models.TextField(blank=True, null=True)
    emergency_phone = models.TextField(blank=True, null=True)
    qr_token = models.UUIDField(default=uuid.uuid4)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="active")
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)
    photo_path = models.TextField(blank=True, null=True)
    emergency2_name = models.TextField(blank=True, null=True)
    emergency2_relationship = models.TextField(blank=True, null=True)
    emergency2_phone = models.TextField(blank=True, null=True)
    qr_scan_count = models.BigIntegerField(default=0)
    last_qr_scan_at = models.DateTimeField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "patients"
        ordering = ["-created_at"]

    def get_sex_display(self):
        value = str(self.sex or "").strip()
        normalized = value.casefold()
        if normalized in {"m", "male", "masculino", "hombre"}:
            return "Masculino"
        if normalized in {"f", "female", "femenino", "mujer"}:
            return "Femenino"
        if normalized in {"o", "other", "otro", "otra", "no binario", "no_binario"}:
            return "Otro"
        return value

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def initials(self):
        return "".join(x[0] for x in [self.first_name, self.last_name] if x)[:2].upper()


class MedicalDocument(ExistingTable):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    patient_id = models.UUIDField()
    owner_id = models.UUIDField()
    document_type = models.TextField(blank=True, null=True)
    original_name = models.TextField(blank=True, null=True)
    storage_path = models.TextField(blank=True, null=True)
    mime_type = models.TextField(blank=True, null=True)
    size_bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "medical_documents"


class Product(ExistingTable):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    name = models.TextField()
    description = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    stock = models.IntegerField(default=0)
    image_url = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)
    colors = TextArrayField(blank=True, default=list)
    sizes = TextArrayField(blank=True, default=list)
    badge = models.TextField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "products"
        ordering = ["-created_at"]


class Order(ExistingTable):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user_id = models.UUIDField()
    order_number = models.TextField(unique=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=40, default="pending")
    payment_method = models.CharField(max_length=40, blank=True, null=True)
    payment_proof_path = models.TextField(blank=True, null=True)
    shipping_address = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)
    payment_rejection_reason = models.TextField(blank=True, null=True)
    payment_reviewed_at = models.DateTimeField(blank=True, null=True)
    payment_reviewed_by = models.UUIDField(blank=True, null=True)
    tracking_number = models.TextField(blank=True, null=True)
    estimated_delivery = models.DateField(blank=True, null=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_code = models.TextField(blank=True, null=True)
    shipping_name = models.TextField(blank=True, null=True)
    shipping_city = models.TextField(blank=True, null=True)
    shipping_postal = models.TextField(blank=True, null=True)
    shipping_phone = models.TextField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "orders"
        ordering = ["-created_at"]


class OrderItem(ExistingTable):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    order_id = models.UUIDField()
    product_id = models.UUIDField(blank=True, null=True)
    quantity = models.IntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    selected_color = models.TextField(blank=True, null=True)
    selected_size = models.TextField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "order_items"


class BankAccount(ExistingTable):
    """Cuenta bancaria publicada por el administrador para los pagos de pacientes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    bank_name = models.TextField()
    account_holder = models.TextField()
    account_number = models.TextField()
    account_type = models.TextField(default="Ahorros")
    tax_id = models.TextField()
    instructions = models.TextField(blank=True, null=True)
    is_visible = models.BooleanField(default=True)
    display_order = models.IntegerField(default=0)
    logo_path = models.TextField(blank=True, null=True)
    qr_path = models.TextField(blank=True, null=True)
    created_by = models.UUIDField(blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "bank_accounts"
        ordering = ["display_order", "bank_name"]


class DiscountCampaign(ExistingTable):
    """Campaña creada por un administrador para repartir tickets limitados."""

    TYPE_CHOICES = [("percentage", "Porcentaje"), ("fixed", "Valor fijo")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    code = models.CharField(max_length=40, unique=True)
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True, null=True)
    discount_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="percentage")
    discount_value = models.DecimalField(max_digits=12, decimal_places=2)
    min_order_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    max_claims = models.PositiveIntegerField(default=1)
    starts_at = models.DateTimeField(blank=True, null=True)
    expires_at = models.DateTimeField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_by = models.UUIDField(blank=True, null=True)
    created_at = models.DateTimeField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "discount_campaigns"
        ordering = ["-created_at"]


class DiscountTicket(ExistingTable):
    """Ticket reclamado por un usuario y utilizable una única vez."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    campaign_id = models.UUIDField()
    user_id = models.UUIDField()
    claimed_at = models.DateTimeField(blank=True, null=True)
    used_at = models.DateTimeField(blank=True, null=True)
    order_id = models.UUIDField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "discount_tickets"
        ordering = ["-claimed_at"]


class NotificationRead(ExistingTable):
    """Marca una notificación calculada como leída para una cuenta."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    user_id = models.UUIDField()
    notification_key = models.CharField(max_length=180)
    read_at = models.DateTimeField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "notification_reads"
        ordering = ["-read_at"]


class Invoice(ExistingTable):
    """Factura interna emitida para un pedido con pago aprobado."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    order_id = models.UUIDField()
    user_id = models.UUIDField()
    invoice_number = models.CharField(max_length=50, unique=True)
    issued_at = models.DateTimeField(blank=True, null=True)
    sent_at = models.DateTimeField(blank=True, null=True)
    created_by = models.UUIDField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "invoices"
        ordering = ["-issued_at"]


class PaymentSetting(ExistingTable):
    id = models.BooleanField(primary_key=True, default=True)
    bank_name = models.TextField(blank=True, null=True)
    account_type = models.TextField(blank=True, null=True)
    account_number = models.TextField(blank=True, null=True)
    interbank_code = models.TextField(blank=True, null=True)
    account_holder = models.TextField(blank=True, null=True)
    tax_id = models.TextField(blank=True, null=True)
    notification_email = models.TextField(blank=True, null=True)
    transfer_qr_payload = models.TextField(blank=True, null=True)
    updated_at = models.DateTimeField(blank=True, null=True)

    class Meta(ExistingTable.Meta):
        db_table = "payment_settings"
