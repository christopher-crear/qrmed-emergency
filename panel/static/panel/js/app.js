document.addEventListener('DOMContentLoaded', () => {
  if (window.lucide) lucide.createIcons();
  const sidebar = document.getElementById('sidebar');
  const menuButton = document.getElementById('menuButton');
  if (window.innerWidth > 760 && localStorage.getItem('qrmed-sidebar') === 'expanded') document.body.classList.add('sidebar-expanded');
  menuButton?.addEventListener('click', () => {
    if (window.innerWidth <= 760) {
      document.body.classList.toggle('sidebar-mobile-open');
      return;
    }
    document.body.classList.toggle('sidebar-expanded');
    localStorage.setItem('qrmed-sidebar', document.body.classList.contains('sidebar-expanded') ? 'expanded' : 'compact');
  });
  const trigger = document.getElementById('profileTrigger'), menu = document.getElementById('profileMenu');
  const notificationButton = document.getElementById('notificationButton'), notificationMenu = document.getElementById('notificationMenu');
  trigger?.addEventListener('click', e => { e.stopPropagation(); notificationMenu?.classList.remove('show'); menu.classList.toggle('show'); });
  notificationButton?.addEventListener('click', e => { e.stopPropagation(); menu?.classList.remove('show'); notificationMenu?.classList.toggle('show'); notificationButton.setAttribute('aria-expanded', notificationMenu?.classList.contains('show') ? 'true' : 'false'); });
  notificationMenu?.addEventListener('click', e => e.stopPropagation());
  document.addEventListener('click', () => { menu?.classList.remove('show'); notificationMenu?.classList.remove('show'); notificationButton?.setAttribute('aria-expanded', 'false'); });

  const search = document.getElementById('globalSearch'), results = document.getElementById('searchResults');
  let timer;
  const runGlobalSearch = async () => {
    const q=search?.value.trim()||'';
    if(q.length===1){ results.innerHTML=''; results.classList.remove('show'); return; }
    try{const endpoint=document.body.dataset.searchUrl||'/buscar/';const r=await fetch(`${endpoint}?q=${encodeURIComponent(q)}`),d=await r.json();
      results.innerHTML=d.results.length?d.results.map(x=>`<a href="${escapeHtml(x.url)}"><strong>${escapeHtml(x.title)}</strong><small>${escapeHtml(x.subtitle)}</small></a>`).join(''):'<div class="no-search">Sin resultados</div>';results.classList.add('show');
    }catch(e){results.classList.remove('show')}
  };
  search?.addEventListener('focus', runGlobalSearch);
  search?.addEventListener('input', () => { clearTimeout(timer); timer=setTimeout(runGlobalSearch,200); });
  results?.addEventListener('click', e => e.stopPropagation());
  document.addEventListener('click', e => { if(!e.target.closest('.global-search')) results?.classList.remove('show'); });

  document.querySelector('[data-close-onboarding]')?.addEventListener('click', () => document.getElementById('medicalOnboarding')?.remove());

  document.querySelectorAll('[data-confirm]').forEach(button => button.addEventListener('click', event => {
    event.preventDefault(); const form=button.closest('form'), dialog=document.getElementById('confirmDialog');
    document.getElementById('confirmTitle').textContent=button.dataset.confirmTitle||'¿Confirmar acción?';
    document.getElementById('confirmText').textContent=button.dataset.confirm||'Esta acción no se puede deshacer.'; dialog.hidden=false;
    dialog.querySelector('[data-confirm-cancel]').onclick=()=>dialog.hidden=true;
    dialog.querySelector('[data-confirm-ok]').onclick=()=>{dialog.hidden=true; form.submit()};
  }));

  const qrModal = document.getElementById('patientQrModal');
  let currentQr = null;
  document.querySelectorAll('[data-open-qr]').forEach(button => button.addEventListener('click', () => {
    currentQr = {name: button.dataset.name, token: button.dataset.token, image: button.dataset.image, publicUrl: button.dataset.public};
    document.getElementById('modalQrImage').src = currentQr.image;
    document.getElementById('modalQrName').textContent = currentQr.name;
    document.getElementById('modalQrToken').textContent = currentQr.token;
    document.getElementById('openPublicQr').href = currentQr.publicUrl;
    qrModal.hidden = false;
    document.body.classList.add('modal-open');
  }));
  const closeQr = () => { if(qrModal){ qrModal.hidden = true; document.body.classList.remove('modal-open'); } };
  qrModal?.querySelector('[data-close-qr]')?.addEventListener('click', closeQr);
  qrModal?.addEventListener('click', event => { if(event.target === qrModal) closeQr(); });
  document.addEventListener('keydown', event => { if(event.key === 'Escape') closeQr(); });
  document.getElementById('downloadQr')?.addEventListener('click', () => {
    if(!currentQr) return; const link=document.createElement('a'); link.href=currentQr.image; link.download=`QR-${currentQr.name.replaceAll(' ','-')}.png`; document.body.appendChild(link); link.click(); link.remove();
  });
  document.getElementById('copyQrLink')?.addEventListener('click', async event => {
    if(!currentQr) return; const absolute=new URL(currentQr.publicUrl,location.origin).href;
    try { await navigator.clipboard.writeText(absolute); event.currentTarget.innerHTML='<span>✓</span> Enlace copiado'; setTimeout(()=>event.currentTarget.innerHTML='<i data-lucide="copy"></i> Copiar enlace',1800); }
    catch(e) { window.prompt('Copia este enlace:', absolute); }
    if(window.lucide) lucide.createIcons();
  });

  document.querySelectorAll('form').forEach(form => {
    const input = form.querySelector('input[name="q"]');
    if (!input) return;
    let autoSearchTimer;
    input.addEventListener('input', () => {
      clearTimeout(autoSearchTimer);
      const query = input.value.trim();
      if (query.length === 1) return;
      if (query.length === 0 || query.length >= 2) autoSearchTimer = setTimeout(() => form.submit(), 380);
    });
  });

  document.querySelectorAll('img[data-image-fallback], img[data-avatar-fallback], img[data-proof-fallback]').forEach(image => {
    const showImageFallback = () => {
      image.hidden = true;
      const fallback = image.nextElementSibling;
      if (fallback) fallback.hidden = false;
      if (window.lucide) lucide.createIcons();
    };
    image.addEventListener('error', showImageFallback, { once: true });
    if (image.complete && image.naturalWidth === 0) showImageFallback();
  });

  document.querySelectorAll('[data-patient-photo-picker]').forEach(picker => {
    const input = picker.querySelector('[data-patient-photo-input]');
    const button = picker.querySelector('[data-patient-photo-button]');
    const preview = picker.querySelector('#patientPhotoPreview');
    let previewUrl = '';
    const openPicker = () => input?.click();

    button?.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      openPicker();
    });
    picker.addEventListener('click', event => {
      if (!event.target.closest('button,input')) openPicker();
    });
    picker.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openPicker();
      }
    });
    input?.addEventListener('change', () => {
      const file = input.files?.[0];
      if (!file || !preview) return;
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = URL.createObjectURL(file);
      preview.src = previewUrl;
      preview.hidden = false;
      const fallback = preview.nextElementSibling;
      if (fallback) fallback.hidden = true;
      picker.classList.add('has-photo');
      if (button) button.lastChild.textContent = ' Cambiar foto';
    });
    window.addEventListener('pagehide', () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    }, { once: true });
  });

  const imageLightbox = document.getElementById('productImageLightbox');
  const sizeGuide = document.getElementById('sizeGuideModal');
  const closeProductModals = () => {
    if (imageLightbox) imageLightbox.hidden = true;
    if (sizeGuide) sizeGuide.hidden = true;
    document.body.classList.remove('modal-open');
  };
  document.querySelectorAll('[data-lightbox-image]').forEach(button => button.addEventListener('click', () => {
    const source = button.dataset.lightboxImage;
    if (!source || !imageLightbox) return;
    document.getElementById('productLightboxImage').src = source;
    document.getElementById('productLightboxTitle').textContent = button.dataset.lightboxTitle || 'Producto';
    imageLightbox.hidden = false;
    document.body.classList.add('modal-open');
  }));
  document.querySelectorAll('[data-open-size-guide]').forEach(button => button.addEventListener('click', event => {
    event.preventDefault();
    if (!sizeGuide) return;
    sizeGuide.hidden = false;
    document.body.classList.add('modal-open');
  }));
  document.querySelectorAll('[data-close-product-modal]').forEach(button => button.addEventListener('click', closeProductModals));
  [imageLightbox, sizeGuide].forEach(modal => modal?.addEventListener('click', event => {
    if (event.target === modal) closeProductModals();
  }));
  document.addEventListener('keydown', event => { if (event.key === 'Escape') closeProductModals(); });
  setTimeout(()=>document.querySelectorAll('.toast').forEach(x=>x.remove()),5000);
});
function escapeHtml(value){const d=document.createElement('div');d.textContent=value??'';return d.innerHTML}
