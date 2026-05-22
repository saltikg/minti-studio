const navToggle = document.querySelector('.nav-toggle');
const navLinks  = document.querySelector('.nav-links');

if (navToggle && navLinks) {
  navToggle.addEventListener('click', function () {
    const isOpen = navLinks.classList.toggle('active');
    navToggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');

    // --- YENİ EKLEDİĞİMİZ KISIM ---
    if (isOpen && window.innerWidth <= 768) {
      // tüm sub-menüleri kapat
      document.querySelectorAll('.nav-links .sub-menu').forEach(function (sm) {
        sm.style.display = 'none';
      });

      // aria-expanded temizle
      document.querySelectorAll('.nav-links .has-sub > .toplink').forEach(function (link) {
        link.setAttribute('aria-expanded', 'false');
      });
    }
  });
}

const hasSubMenuItems = document.querySelectorAll('.nav-links .has-sub > .toplink');

hasSubMenuItems.forEach(function (item) {
  item.addEventListener('click', function (event) {
    if (window.innerWidth <= 768) {
      event.preventDefault();

      const parentLi = this.parentElement;
      const subMenu  = parentLi.querySelector('.sub-menu');
      if (!subMenu) return;

      const isVisible = subMenu.style.display === 'block';

      // başka açık varsa kapat (opsiyonel ama güzel UX)
      document.querySelectorAll('.nav-links .sub-menu').forEach(function (other) {
        other.style.display = 'none';
      });

      // yeniden toggle et
      if (!isVisible) {
        subMenu.style.display = 'block';
      } else {
        subMenu.style.display = 'none';
      }
    }
  });
});

// desktop'a dönünce inline display'leri temizle
window.addEventListener('resize', function () {
  if (window.innerWidth > 768) {
    if (navLinks) {
      navLinks.classList.remove('active');
    }
    document.querySelectorAll('.sub-menu').forEach(function (sm) {
      sm.style.display = '';
    });
  }
});
