import re
from functools import lru_cache

from django import forms
from django.db import connection

from .models import BankAccount, Order, Patient, PaymentSetting, Product, Profile
from .value_utils import humanize_value


DEFAULT_SEX_CHOICES = (
    ("male", "Masculino"),
    ("female", "Femenino"),
    ("other", "Otro"),
)


def sex_label(value):
    normalized = str(value or "").strip().casefold()
    if normalized in {"m", "male", "masculino", "hombre"}:
        return "Masculino"
    if normalized in {"f", "female", "femenino", "mujer"}:
        return "Femenino"
    if normalized in {"o", "other", "otro", "otra", "no binario", "no_binario"}:
        return "Otro"
    return str(value or "").strip().capitalize()


def _known_sex_family(values):
    """Completa una familia reconocida cuando solo existen algunos registros."""
    families = (
        ("M", "F", "O"),
        ("male", "female", "other"),
        ("masculino", "femenino", "otro"),
        ("Masculino", "Femenino", "Otro"),
    )
    value_set = {str(value).strip() for value in values if str(value).strip()}
    for family in families:
        if value_set.intersection(family):
            return family
    return tuple(value_set)


@lru_cache(maxsize=1)
def patient_sex_choices():
    """Obtiene una sola vez los valores reales de ``patients_sex_check``.

    El esquema de Supabase ya existe y algunos proyectos usan M/F/O, otros
    male/female/other y otros etiquetas en español. Consultar el catálogo evita
    traducir el valor enviado y volver a violar la restricción.
    """
    if connection.vendor != "postgresql":
        return DEFAULT_SEX_CHOICES

    values = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_get_constraintdef(c.oid)
                  FROM pg_constraint c
                  JOIN pg_class t ON t.oid = c.conrelid
                  JOIN pg_namespace n ON n.oid = t.relnamespace
                 WHERE n.nspname = 'public'
                   AND t.relname = 'patients'
                   AND c.conname = 'patients_sex_check'
                   AND c.contype = 'c'
                 LIMIT 1
                """
            )
            row = cursor.fetchone()
        if row and row[0]:
            values = [
                item.replace("''", "'")
                for item in re.findall(r"'((?:''|[^'])*)'", row[0])
            ]
    except Exception:
        values = []

    # Respaldo para un CHECK con una expresión no estándar: los valores ya
    # guardados necesariamente son válidos y permiten reconocer su familia.
    if not values:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT sex
                      FROM public.patients
                     WHERE sex IS NOT NULL AND btrim(sex) <> ''
                     LIMIT 20
                    """
                )
                values = [row[0] for row in cursor.fetchall()]
            values = list(_known_sex_family(values))
        except Exception:
            values = []

    if not values:
        return DEFAULT_SEX_CHOICES

    unique_values = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    order = {"Masculino": 0, "Femenino": 1, "Otro": 2}
    unique_values.sort(key=lambda value: (order.get(sex_label(value), 99), value.casefold()))
    return tuple((value, sex_label(value)) for value in unique_values)


class StyledFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "checkbox")
            else:
                field.widget.attrs.setdefault("class", "form-control")

        # Los datos heredados pueden venir serializados como JSON, arrays de
        # PostgreSQL o representaciones de Python. En los inputs mostramos solo
        # el contenido legible, nunca llaves/corchetes de serialización.
        if not self.is_bound:
            for name, field in self.fields.items():
                if not isinstance(field, forms.CharField) or isinstance(field, forms.ChoiceField):
                    continue
                raw_value = self.initial.get(name)
                if raw_value is None and getattr(self, "instance", None) is not None:
                    raw_value = getattr(self.instance, name, None)
                if raw_value not in (None, ""):
                    self.initial[name] = humanize_value(raw_value)


class PatientPersonalForm(StyledFormMixin, forms.ModelForm):
    sex = forms.ChoiceField(label="Sexo", choices=(), required=True)

    photo = forms.ImageField(
        required=False,
        label="Fotografía del paciente",
        widget=forms.ClearableFileInput(attrs={
            "accept": "image/jpeg,image/png,image/webp",
            "data-patient-photo-input": "",
        }),
    )

    class Meta:
        model = Patient
        fields = ["first_name", "last_name", "id_number", "birth_date", "sex", "phone", "email", "address", "city", "status"]
        widgets = {
            "first_name": forms.TextInput(),
            "last_name": forms.TextInput(),
            "id_number": forms.TextInput(),
            "birth_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
            "phone": forms.TextInput(attrs={"type": "tel"}),
            "email": forms.EmailInput(),
            "address": forms.TextInput(),
            "city": forms.TextInput(),
            "status": forms.Select(choices=Patient.STATUS_CHOICES),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["birth_date"].input_formats = ["%Y-%m-%d"]
        choices = list(patient_sex_choices())
        current_sex = getattr(self.instance, "sex", "") if self.instance else ""
        current_sex = str(current_sex or "").strip()
        if current_sex and current_sex not in {value for value, _ in choices}:
            choices.append((current_sex, sex_label(current_sex)))
        self.fields["sex"].choices = choices
        if current_sex:
            # Se conserva exactamente el valor que ya aprobó el CHECK de la BD.
            self.initial["sex"] = current_sex

    def clean_sex(self):
        value = str(self.cleaned_data.get("sex") or "").strip()
        if not value:
            raise forms.ValidationError("Selecciona un sexo valido.")
        return value

    def clean_photo(self):
        photo = self.cleaned_data.get("photo")
        if not photo:
            return photo
        if photo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("La fotografía no puede superar los 5 MB.")
        allowed = {"image/jpeg", "image/png", "image/webp"}
        if getattr(photo, "content_type", "") not in allowed:
            raise forms.ValidationError("Selecciona una imagen JPG, PNG o WebP válida.")
        return photo


class PatientMedicalForm(StyledFormMixin, forms.ModelForm):
    allergies = forms.CharField(required=False, widget=forms.TextInput(attrs={"placeholder": "Penicilina, látex, mariscos..."}))
    diseases = forms.CharField(required=False, widget=forms.TextInput(attrs={"placeholder": "Hipertensión, diabetes tipo 2..."}))
    medications = forms.CharField(required=False, widget=forms.TextInput(attrs={"placeholder": "Metformina 500 mg..."}))

    class Meta:
        model = Patient
        fields = ["blood_type", "insurance", "allergies", "diseases", "medications", "disabilities", "history", "notes"]
        widgets = {
            "blood_type": forms.Select(choices=[(x, x) for x in ["O+", "O-", "A+", "A-", "B+", "B-", "AB+", "AB-"]]),
            "insurance": forms.TextInput(attrs={"placeholder": "Ej. IESS"}),
            "disabilities": forms.TextInput(attrs={"placeholder": "Ninguna"}),
            "history": forms.TextInput(attrs={"placeholder": "Cirugías, hospitalizaciones..."}),
            "notes": forms.TextInput(attrs={"placeholder": "Información adicional..."}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and not self.is_bound:
            for name in ("allergies", "diseases", "medications"):
                self.fields[name].initial = humanize_value(getattr(self.instance, name, None))
            for name in ("insurance", "disabilities", "history", "notes"):
                self.fields[name].initial = humanize_value(getattr(self.instance, name, None))

    def save(self, commit=True):
        instance = super().save(commit=False)
        for name in ("allergies", "diseases", "medications"):
            readable = humanize_value(self.cleaned_data.get(name, ""))
            setattr(instance, name, [x.strip() for x in readable.split(",") if x.strip()])
        for name in ("insurance", "disabilities", "history", "notes"):
            setattr(instance, name, humanize_value(self.cleaned_data.get(name, ""), default=""))
        if commit:
            instance.save()
        return instance


class PatientEmergencyForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Patient
        fields = ["emergency_name", "emergency_relationship", "emergency_phone", "emergency2_name", "emergency2_relationship", "emergency2_phone"]
        widgets = {
            "emergency_name": forms.TextInput(),
            "emergency_relationship": forms.TextInput(),
            "emergency_phone": forms.TextInput(attrs={"type": "tel"}),
            "emergency2_name": forms.TextInput(),
            "emergency2_relationship": forms.TextInput(),
            "emergency2_phone": forms.TextInput(attrs={"type": "tel"}),
        }

    def clean(self):
        data = super().clean()
        for name in ("emergency_name", "emergency_relationship", "emergency_phone"):
            if not data.get(name):
                self.add_error(name, "Este campo es obligatorio para el contacto principal.")
        return data


class ProductForm(StyledFormMixin, forms.ModelForm):
    colors_text = forms.CharField(required=False, label="Colores (separados por comas)")
    sizes_text = forms.CharField(required=False, label="Tallas (separadas por comas)")

    class Meta:
        model = Product
        fields = ["name", "price", "stock", "image_url", "badge", "description", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "Ej. Pulsera médica premium"}),
            "price": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "stock": forms.NumberInput(attrs={"min": "0"}),
            "image_url": forms.TextInput(attrs={"placeholder": "URL pública o ruta de Supabase Storage"}),
            "badge": forms.TextInput(attrs={"placeholder": "Ej. Más popular"}),
            "description": forms.Textarea(attrs={"rows": 3, "placeholder": "Describe las características del producto..."}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["colors_text"].initial = ", ".join(self.instance.colors or [])
            self.fields["sizes_text"].initial = ", ".join(self.instance.sizes or [])

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.colors = [x.strip() for x in self.cleaned_data.get("colors_text", "").split(",") if x.strip()]
        instance.sizes = [x.strip() for x in self.cleaned_data.get("sizes_text", "").split(",") if x.strip()]
        if commit:
            instance.save()
        return instance


class OrderUpdateForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Order
        fields = ["status", "estimated_delivery", "tracking_number"]
        widgets = {
            "status": forms.Select(choices=[("pending", "Pendiente"), ("confirmed", "Confirmado"), ("production", "En producción"), ("shipped", "Enviado"), ("delivered", "Entregado"), ("cancelled", "Cancelado")]),
            "estimated_delivery": forms.DateInput(attrs={"type": "date"}),
            "tracking_number": forms.TextInput(attrs={"readonly": True}),
        }


class ProfileForm(StyledFormMixin, forms.ModelForm):
    avatar = forms.ImageField(
        required=False,
        widget=forms.ClearableFileInput(
            attrs={"accept": "image/jpeg,image/png,image/webp"}
        ),
    )
    cover = forms.ImageField(
        required=False,
        widget=forms.ClearableFileInput(
            attrs={"accept": "image/jpeg,image/png,image/webp"}
        ),
    )

    class Meta:
        model = Profile
        fields = ["full_name", "phone", "city", "specialty"]
        # Estas columnas son ``text`` en Supabase. Sin widgets explícitos
        # Django las representa como ``textarea`` aunque sean datos breves.
        widgets = {
            "full_name": forms.TextInput(attrs={"autocomplete": "name"}),
            "phone": forms.TextInput(attrs={"type": "tel", "autocomplete": "tel"}),
            "city": forms.TextInput(attrs={"autocomplete": "address-level2"}),
            "specialty": forms.TextInput(attrs={"autocomplete": "organization-title"}),
        }


class BankAccountForm(StyledFormMixin, forms.ModelForm):
    logo = forms.ImageField(
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
    )
    qr_image = forms.ImageField(
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp"}),
    )

    class Meta:
        model = BankAccount
        fields = [
            "bank_name", "account_holder", "account_number", "account_type",
            "tax_id", "display_order", "instructions", "is_visible",
        ]
        widgets = {
            "bank_name": forms.TextInput(attrs={
                "placeholder": "Ej. Banco de Loja",
                "list": "bank-name-options",
                "autocomplete": "organization",
            }),
            "account_holder": forms.TextInput(attrs={"placeholder": "Ej. QRMed Emergency"}),
            "account_number": forms.TextInput(attrs={
                "placeholder": "Ej. 2200123456",
                "inputmode": "numeric",
                "autocomplete": "off",
            }),
            "account_type": forms.Select(choices=[
                ("Ahorros", "Ahorros"),
                ("Corriente", "Corriente"),
            ]),
            "tax_id": forms.TextInput(attrs={
                "placeholder": "Cédula o RUC",
                "inputmode": "numeric",
                "autocomplete": "off",
            }),
            "display_order": forms.NumberInput(attrs={"min": 0, "step": 1}),
            "instructions": forms.Textarea(attrs={
                "rows": 3,
                "placeholder": "Indicaciones opcionales para realizar la transferencia",
            }),
            "is_visible": forms.CheckboxInput(),
        }

    def clean_bank_name(self):
        value = (self.cleaned_data.get("bank_name") or "").strip()
        if not value:
            raise forms.ValidationError("Ingresa el nombre del banco o cooperativa.")
        return value

    def clean_account_holder(self):
        value = (self.cleaned_data.get("account_holder") or "").strip()
        if not value:
            raise forms.ValidationError("Ingresa el titular de la cuenta.")
        return value

    def clean_account_number(self):
        value = re.sub(r"\s+", "", self.cleaned_data.get("account_number") or "")
        if not value:
            raise forms.ValidationError("Ingresa el número de cuenta.")
        if len(value) > 40:
            raise forms.ValidationError("El número de cuenta es demasiado largo.")
        return value

    def clean_tax_id(self):
        value = re.sub(r"\s+", "", self.cleaned_data.get("tax_id") or "")
        if not value:
            raise forms.ValidationError("Ingresa la cédula o RUC del titular.")
        return value


class PaymentSettingForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = PaymentSetting
        fields = ["bank_name", "account_type", "account_number", "interbank_code", "account_holder", "tax_id", "notification_email", "transfer_qr_payload"]
        widgets = {"transfer_qr_payload": forms.Textarea(attrs={"rows": 3})}
