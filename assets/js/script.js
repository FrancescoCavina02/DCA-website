document.addEventListener('DOMContentLoaded', function() {
  const btn = document.getElementById('menu-toggle');
  const nav = document.getElementById('nav-menu');
  const overlay = document.getElementById('overlay');
  const dropdowns = nav.querySelectorAll('.dropdown');
  
  // Toggle menu function
  function toggleMenu() {
    nav.classList.toggle('active');
    overlay.classList.toggle('active');
  }

  // Event listeners
  btn.addEventListener('click', function(e) {
    e.preventDefault();
    toggleMenu();
  });

  overlay.addEventListener('click', function() {
    toggleMenu();
  });

  // Mobile dropdown functionality with accordion system and content pushing
  dropdowns.forEach(dropdown => {
    const dropdownSpan = dropdown.querySelector('span');
    const dropdownContent = dropdown.querySelector('.dropdown-content');
    
    dropdownSpan.addEventListener('click', function(e) {
      if (window.innerWidth <= 768) {
        e.preventDefault();
        e.stopPropagation();
        
        const isCurrentlyActive = dropdown.classList.contains('active');
        
        // First, close all dropdowns and reset all positions immediately
        dropdowns.forEach(otherDropdown => {
          otherDropdown.classList.remove('active');
          const otherContent = otherDropdown.querySelector('.dropdown-content');
          if (otherContent) {
            otherContent.style.maxHeight = '0px';
            otherContent.style.opacity = '0';
            // Reset child links
            otherContent.querySelectorAll('a').forEach(link => {
              link.style.opacity = '0';
              link.style.transform = 'translateX(-20px)';
            });
          }
        });
        
        // Reset ALL menu item positions immediately
        const allMenuItems = Array.from(nav.children);
        allMenuItems.forEach(item => {
          item.style.transform = '';
          item.style.transition = 'transform 0.5s cubic-bezier(0.175, 0.885, 0.32, 1.275)';
        });
        
        // Force layout recalculation
        nav.offsetHeight;
        
        // If clicking on an inactive dropdown, open it
        if (!isCurrentlyActive) {
          
          // Step 1: Measure content height while completely hidden
          dropdownContent.style.position = 'absolute';
          dropdownContent.style.visibility = 'hidden';
          dropdownContent.style.maxHeight = 'none';
          dropdownContent.style.opacity = '1';
          const contentHeight = dropdownContent.scrollHeight;
          
          // Step 2: Hide it again immediately
          dropdownContent.style.position = '';
          dropdownContent.style.visibility = '';
          dropdownContent.style.maxHeight = '0px';
          dropdownContent.style.opacity = '0';
          
          // Step 3: Find items to move
          const currentDropdownIndex = allMenuItems.indexOf(dropdown);
          const itemsToMove = allMenuItems.slice(currentDropdownIndex + 1);
          
          // Step 4: Use requestAnimationFrame to ensure proper timing
          requestAnimationFrame(() => {
            // FIRST: Move items down (content pushing)
            itemsToMove.forEach(item => {
              item.style.transform = `translateY(${contentHeight}px)`;
            });
            
            // SECOND: Activate dropdown class for styling
            dropdown.classList.add('active');
            
            // THIRD: Make dropdown content visible
            requestAnimationFrame(() => {
              dropdownContent.style.maxHeight = contentHeight + 'px';
              dropdownContent.style.opacity = '1';
              
              // Celebration effect
              dropdownSpan.style.transform = 'scale(1.05)';
              setTimeout(() => {
                dropdownSpan.style.transform = '';
              }, 200);
              
              // Animate dropdown items
              const dropdownLinks = dropdownContent.querySelectorAll('a');
              dropdownLinks.forEach((link, index) => {
                link.style.opacity = '0';
                link.style.transform = 'translateX(-20px)';
                link.style.transition = 'all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275)';
                
                setTimeout(() => {
                  link.style.opacity = '1';
                  link.style.transform = 'translateX(0)';
                }, 100 + (index * 80));
              });
            });
          });
          
        } else {
          // Just add bounce effect for closing
          dropdownSpan.style.transform = 'scale(0.95)';
          setTimeout(() => {
            dropdownSpan.style.transform = '';
          }, 150);
        }
      }
    });
  });

  // Close menu when clicking a link
  const navLinks = nav.getElementsByTagName('a');
  for (let link of navLinks) {
    link.addEventListener('click', function() {
      if (link.getAttribute('href').startsWith('#') || link.getAttribute('href').endsWith('.html')) {
          toggleMenu();
      }
    });
  }
}); 