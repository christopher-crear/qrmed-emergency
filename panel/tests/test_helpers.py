import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django import forms
from django.test import Client, SimpleTestCase, override_settings
from django.urls import reverse

from panel.forms import (
    PatientEmergencyForm, PatientMedicalForm, PatientPersonalForm,
    ProfileForm, patient_sex_choices, validate_ecuador_cedula,
)
from panel.patient_utils import medical_profile_is_complete
from panel.models import Patient
from panel.services import (
    SupabaseError, normalize_storage_path, public_storage_url, sign_in,
    storage_image_bytes, storage_image_candidates, storage_image_signed_url,
    storage_signed_url, upload_file, versioned_media_url,
)
from panel.templatetags.panel_extras import initials, money, payment_method_label, status_label, status_step
from panel.views import _customer_avatar_url


class TemplateFilterTests(SimpleTestCase):
    def test_money_uses_ecuadorian_format(self):
        self.assertEqual(money("1234.50"), "$1.234,50")

    def test_status_helpers(self):
        self.assertEqual(status_label("production"), "En producción")
        self.assertEqual(status_step("shipped"), 3)

    def test_payment_method_variants(self):
        self.assertEqual(payment_method_label("bank_transfer"), "Transferencia")
        self.assertEqual(payment_method_label("depósito"), "Depósito")

    def test_initials_uses_the_first_two_names(self):
        self.assertEqual(initials("Rafael Alexander Eras Granda"), "RA")
        self.assertEqual(initials("Christopher"), "CH")
        self.assertEqual(initials(""), "?")


class AccountAvatarTests(SimpleTestCase):
    def test_profile_avatar_is_preferred_over_patient_photo(self):
        profile = SimpleNamespace(id=uuid.uuid4())
        patient = SimpleNamespace(id=uuid.uuid4(), photo_path="patient/photo.png")

        self.assertEqual(
            _customer_avatar_url(patient, profile),
            reverse("profile_avatar_image", kwargs={"profile_id": profile.id}),
        )

    def test_patient_photo_is_only_used_without_linked_profile(self):
        patient = SimpleNamespace(id=uuid.uuid4(), photo_path="patient/photo.png")

        self.assertEqual(
            _customer_avatar_url(patient, None),
            reverse("patient_photo_image", kwargs={"patient_id": patient.id}),
        )


class PatientFormTests(SimpleTestCase):
    def test_ecuadorian_cedula_checksum(self):
        self.assertEqual(validate_ecuador_cedula("1104091689"), "1104091689")
        with self.assertRaises(forms.ValidationError):
            validate_ecuador_cedula("1104091688")

    def test_qr_requires_a_complete_medical_profile(self):
        patient = Patient(
            first_name="Christopher", last_name="Eras", id_number="1104091689",
            birth_date="2007-03-02", sex="male", phone="0989414258",
            email="cderas@example.com", address="Loja", city="Loja", blood_type="O+",
            emergency_name="Contacto", emergency_relationship="Madre",
            emergency_phone="0987654321",
        )
        self.assertTrue(medical_profile_is_complete(patient))
        patient.blood_type = ""
        self.assertFalse(medical_profile_is_complete(patient))
    def test_sex_uses_database_values_and_spanish_labels(self):
        form = PatientPersonalForm()
        choices = dict(form.fields["sex"].choices)
        self.assertEqual(choices["male"], "Masculino")
        self.assertEqual(choices["female"], "Femenino")
        self.assertNotIn("Masculino", choices)

    def test_sex_choices_follow_the_real_postgresql_check_constraint(self):
        cursor_context = MagicMock()
        cursor = cursor_context.__enter__.return_value
        cursor.fetchone.return_value = (
            "CHECK ((sex = ANY (ARRAY['M'::text, 'F'::text, 'O'::text])))",
        )
        database = Mock(vendor="postgresql")
        database.cursor.return_value = cursor_context
        patient_sex_choices.cache_clear()
        try:
            with patch("panel.forms.connection", database):
                self.assertEqual(
                    patient_sex_choices(),
                    (("M", "Masculino"), ("F", "Femenino"), ("O", "Otro")),
                )
        finally:
            patient_sex_choices.cache_clear()

    def test_existing_sex_is_preserved_without_guessing_a_database_value(self):
        patient = Patient(sex="Masculino")
        form = PatientPersonalForm(instance=patient)
        self.assertEqual(form.initial["sex"], "Masculino")
        self.assertIn(("Masculino", "Masculino"), form.fields["sex"].choices)

    def test_personal_fields_are_regular_single_line_inputs(self):
        form = PatientPersonalForm()
        for field_name in (
            "first_name", "last_name", "id_number", "phone", "email",
            "address", "city",
        ):
            self.assertNotIsInstance(form.fields[field_name].widget, forms.Textarea)
            self.assertIsInstance(form.fields[field_name].widget, forms.widgets.Input)
        self.assertNotIn("<textarea", form.as_p())

    def test_medical_fields_are_regular_inputs(self):
        form = PatientMedicalForm()
        for field_name in (
            "insurance", "allergies", "diseases", "medications",
            "disabilities", "history", "notes",
        ):
            self.assertIsInstance(form.fields[field_name].widget, forms.TextInput)
        self.assertNotIn("<textarea", form.as_p())

    def test_emergency_fields_are_regular_single_line_inputs(self):
        form = PatientEmergencyForm()
        self.assertNotIn("<textarea", form.as_p())

    def test_personal_form_saves_the_sex_value_accepted_by_supabase(self):
        form = PatientPersonalForm(data={
            "first_name": "Christopher",
            "last_name": "Granda",
            "id_number": "1104091689",
            "birth_date": "2007-03-02",
            "sex": "male",
            "phone": "+593989414258",
            "email": "cderas@example.com",
            "address": "Loja",
            "city": "Loja",
            "status": "active",
        }, instance=Patient())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save(commit=False).sex, "male")


class ProfileFormTests(SimpleTestCase):
    def test_profile_fields_are_single_line_inputs(self):
        form = ProfileForm()
        for field_name in ("full_name", "phone", "city", "specialty"):
            self.assertIsInstance(form.fields[field_name].widget, forms.TextInput)
            self.assertNotIsInstance(form.fields[field_name].widget, forms.Textarea)
        self.assertNotIn("<textarea", form.as_p())


class DemoServiceTests(SimpleTestCase):
    @override_settings(DEMO_MODE=False)
    def test_login_page_renders_with_global_context_processor(self):
        response = Client().get("/login/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bienvenido de nuevo")

    @override_settings(DEMO_MODE=True, DEMO_ADMIN_EMAIL="admin@qrmed.ec", DEMO_ADMIN_PASSWORD="admin123")
    def test_demo_login(self):
        result = sign_in("admin@qrmed.ec", "admin123")
        self.assertEqual(result["user"]["id"], "11111111-1111-1111-1111-111111111111")

    @override_settings(DEMO_MODE=True, DEMO_ADMIN_EMAIL="admin@qrmed.ec", DEMO_ADMIN_PASSWORD="admin123")
    def test_demo_login_rejects_invalid_password(self):
        with self.assertRaises(SupabaseError):
            sign_in("admin@qrmed.ec", "incorrecta")

    @override_settings(DEMO_MODE=False, SUPABASE_URL="https://demo.supabase.co")
    def test_public_storage_url(self):
        self.assertEqual(public_storage_url("avatars/a.png", "profiles"), "https://demo.supabase.co/storage/v1/object/public/profiles/avatars/a.png")

    def test_storage_path_accepts_bucket_prefix_and_supabase_url(self):
        self.assertEqual(normalize_storage_path("patient-files/patients/a.png", "patient-files"), "patients/a.png")
        self.assertEqual(
            normalize_storage_path(
                "https://demo.supabase.co/storage/v1/object/public/payment-proofs/orders/proof.png",
                "payment-proofs",
            ),
            "orders/proof.png",
        )

    @override_settings(
        DEMO_MODE=False,
        SUPABASE_URL="https://demo.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="server-secret",
    )
    @patch("panel.services.requests.post")
    def test_private_storage_uses_signed_url(self, post):
        cache.clear()
        response = Mock(ok=True)
        response.json.return_value = {"signedURL": "/object/sign/payment-proofs/proof.png?token=abc"}
        post.return_value = response
        self.assertEqual(
            storage_signed_url("proof.png", "payment-proofs"),
            "https://demo.supabase.co/storage/v1/object/sign/payment-proofs/proof.png?token=abc",
        )

    @override_settings(
        DEMO_MODE=False,
        SUPABASE_URL="https://demo.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="sb_secret_example",
    )
    @patch("panel.services.requests.post")
    def test_new_secret_key_is_not_sent_as_bearer(self, post):
        cache.clear()
        response = Mock(ok=True)
        response.json.return_value = {"signedURL": "/object/sign/profiles/avatar.png?token=abc"}
        post.return_value = response
        storage_signed_url("profiles/avatar.png", "profiles")
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["apikey"], "sb_secret_example")
        self.assertNotIn("Authorization", headers)

    @override_settings(DEMO_MODE=False)
    @patch("panel.services._storage_matches_from_api")
    @patch("panel.services._storage_matches_from_database")
    def test_avatar_recovery_never_selects_cover(self, database_matches, api_matches):
        cache.clear()
        database_matches.return_value = ["user-id/avatar-real.jpg"]
        api_matches.return_value = []
        result = storage_image_candidates(
            "profile-images", "", identifiers=("user-id",), keywords=("avatar",)
        )
        self.assertEqual(result, ["user-id/avatar-real.jpg"])
        database_matches.assert_called_once_with("profile-images", ("user-id",), ("avatar",))

    @override_settings(
        DEMO_MODE=False,
        SUPABASE_URL="https://demo.supabase.co",
        SUPABASE_SERVICE_ROLE_KEY="legacy-service-jwt",
    )
    @patch("panel.services.storage_signed_url")
    @patch("panel.services.requests.get")
    def test_private_image_can_be_downloaded_through_signed_fallback(self, get, signed_url):
        cache.clear()
        signed_url.return_value = (
            "https://demo.supabase.co/storage/v1/object/sign/profile-images/user/avatar.jpg?token=abc"
        )
        failed = Mock(ok=False)
        success = Mock(ok=True, content=b"image-bytes", headers={"content-type": "image/jpeg"})
        get.side_effect = [failed, failed, success]
        result = storage_image_bytes(
            "profile-images", "user/avatar.jpg", identifiers=("user",), keywords=("avatar",)
        )
        self.assertEqual(result, (b"image-bytes", "image/jpeg"))
        self.assertEqual(get.call_count, 3)
        self.assertEqual(get.call_args.kwargs["headers"], {})

    @override_settings(DEMO_MODE=False)
    @patch("panel.services.storage_signed_url")
    @patch("panel.services.storage_image_candidates")
    def test_image_route_prefers_direct_signed_url(self, candidates, signed_url):
        candidates.return_value = ["owner/patient/photo.png"]
        signed_url.return_value = (
            "https://demo.supabase.co/storage/v1/object/sign/patient-photos/owner/patient/photo.png?token=abc"
        )
        result = storage_image_signed_url(
            "patient-photos", "owner/patient/photo.png", identifiers=("patient",)
        )
        self.assertIn("/object/sign/patient-photos/", result)

    @override_settings(
        DEMO_MODE=False,
        SUPABASE_URL="https://demo.supabase.co",
        SUPABASE_ANON_KEY="anon-jwt",
        SUPABASE_SERVICE_ROLE_KEY="service-jwt",
    )
    @patch("panel.services.requests.post")
    def test_storage_retries_with_service_key_after_session_rejection(self, post):
        cache.clear()
        rejected = Mock(ok=False, status_code=401, text='{"message":"JWT expired"}')
        accepted = Mock(ok=True)
        accepted.json.return_value = {"signedURL": "/object/sign/profile-images/user/avatar.png?token=abc"}
        post.side_effect = [rejected, accepted]
        result = storage_signed_url(
            "user/avatar.png", "profile-images", access_token="expired-user-token"
        )
        self.assertIn("/object/sign/profile-images/", result)
        self.assertEqual(post.call_count, 2)

    @override_settings(
        DEMO_MODE=False,
        SUPABASE_URL="https://demo.supabase.co",
        SUPABASE_ANON_KEY="anon-jwt",
        SUPABASE_SERVICE_ROLE_KEY="service-jwt",
    )
    @patch("panel.services.requests.post")
    def test_patient_photo_upload_retries_after_expired_session(self, post):
        rejected = Mock(ok=False, status_code=401, text='{"message":"JWT expired"}')
        accepted = Mock(ok=True, status_code=200)
        post.side_effect = [rejected, accepted]
        photo = SimpleUploadedFile("paciente.png", b"image-bytes", content_type="image/png")
        # ImageField/Pillow validates the image before the storage helper runs.
        # Simulate that inspection leaving the stream at EOF.
        photo.read()

        stored_path = upload_file(
            photo, "patient-photos", "owner-id/patient-id", "photo-",
            access_token="expired-user-token",
        )

        self.assertTrue(stored_path.startswith("owner-id/patient-id/photo-"))
        self.assertTrue(stored_path.endswith(".png"))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["data"], b"image-bytes")

    def test_versioned_media_url_changes_when_profile_is_updated(self):
        updated_at = datetime(2026, 8, 14, 12, 30, tzinfo=timezone.utc)
        result = versioned_media_url("/media/perfiles/user/avatar/", updated_at)

        self.assertRegex(result, r"/media/perfiles/user/avatar/\?v=\d+")
