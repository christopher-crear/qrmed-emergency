# Activar Google en Supabase

El inicio de sesión con Facebook fue retirado. El sistema conserva únicamente Google como proveedor social opcional.

## 1. URL de retorno de QRMed

En **Supabase → Authentication → URL Configuration** agrega en **Redirect URLs**:

```text
https://qrmed-emergency-yp3g.onrender.com/login/social/callback/
```

Para probar en local agrega también:

```text
http://127.0.0.1:8000/login/social/callback/
http://localhost:8000/login/social/callback/
```

## 2. Google

1. En Google Cloud crea credenciales OAuth de tipo aplicación web.
2. En sus URI de redirección autorizados agrega la URL que muestra Supabase para Google. Normalmente es:

   ```text
   https://TU_PROJECT_REF.supabase.co/auth/v1/callback
   ```

3. En **Supabase → Authentication → Providers → Google**, activa el proveedor y pega el Client ID y Client Secret.

## Flujo implementado

- Una cuenta nueva se registra como usuario/paciente, nunca como administrador.
- Se guardan nombre, correo y fotografía proporcionados por Google.
- Se crea una ficha médica provisional asociada al usuario.
- Al entrar por primera vez aparece una ventana solicitando completar la ficha médica.
- La ventana deja de aparecer después de terminar los tres pasos de la ficha.

No agregues el Client Secret de Google a Render: ese secreto permanece guardado en Supabase.
