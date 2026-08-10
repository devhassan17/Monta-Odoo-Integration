/** @odoo-module **/

function csrfToken() {
    return (window.odoo && odoo.csrf_token) || '';
}

async function rpc(route, params) {
    const res = await fetch(route, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken(),
        },
        body: JSON.stringify({ jsonrpc: '2.0', method: 'call', id: 1, params }),
    });
    const data = await res.json();
    return data.result;
}

function initMontaPickup() {
    const speedContainer = document.querySelector('.monta-delivery-speed-container');
    const pickupContainer = document.querySelector('.monta-pickup-container');
    if (!speedContainer && !pickupContainer) return;

    // Prevent Odoo checkout card-click bubbling inside our sections
    [speedContainer, pickupContainer].forEach(el => {
        if (el) el.addEventListener('click', e => e.stopPropagation());
    });

    const togglePickup = document.querySelector('#monta_use_pickup');
    const box = document.querySelector('#monta-pickup-box');
    const btnSearch = document.querySelector('#btn_search_monta_pickup');
    const streetInput = document.querySelector('#monta_pickup_street');
    const houseInput = document.querySelector('#monta_pickup_house');
    const zipInput = document.querySelector('#monta_pickup_zip');
    const cityInput = document.querySelector('#monta_pickup_city');
    const countrySelect = document.querySelector('#monta_pickup_country');
    const loading = document.querySelector('#monta_pickup_loading');
    const errorDiv = document.querySelector('#monta_pickup_error');
    const resultsDiv = document.querySelector('#monta_pickup_results');

    // ── Auto-detect address from checkout form ──
    function autoDetectUserAddress() {
        const domStreet = document.querySelector('input[name="street"], #street');
        const domHouse = document.querySelector('input[name="street2"], #street2');
        const domZip = document.querySelector('input[name="zip"], #zip');
        const domCity = document.querySelector('input[name="city"], #city');

        if (streetInput && domStreet && domStreet.value && !streetInput.value) streetInput.value = domStreet.value.trim();
        if (houseInput && domHouse && domHouse.value && !houseInput.value) houseInput.value = domHouse.value.trim();
        if (zipInput && domZip && domZip.value && !zipInput.value) zipInput.value = domZip.value.trim();
        if (cityInput && domCity && domCity.value && !cityInput.value) cityInput.value = domCity.value.trim();
    }

    autoDetectUserAddress();

    // ── Delivery Speed: div row click → check radio ──
    if (speedContainer) {
        speedContainer.querySelectorAll('.monta-option-row').forEach(row => {
            row.addEventListener('click', () => {
                const radio = row.querySelector('input.monta-delivery-radio');
                if (radio && !radio.checked) {
                    radio.checked = true;
                    radio.dispatchEvent(new Event('change', { bubbles: true }));
                }
            });
        });
    }

    // ── Delivery Speed: radio change → call backend ──
    document.querySelectorAll('input.monta-delivery-radio').forEach(radio => {
        radio.addEventListener('change', async () => {
            if (!radio.checked) return;
            const selectedType = radio.value;

            // Sync active class immediately for visual feedback
            if (speedContainer) {
                speedContainer.querySelectorAll('.monta-delivery-card').forEach(row => {
                    const r = row.querySelector('input[type="radio"]');
                    row.classList.toggle('monta-delivery-card--active', r === radio);
                });
            }

            showLoading(true);
            try {
                const res = await rpc('/shop/monta/select_delivery_type', { delivery_type: selectedType });
                if (res && res.status === 'success') {
                    window.location.reload();
                } else {
                    showLoading(false);
                }
            } catch (e) {
                showLoading(false);
                console.error('Failed to set delivery type:', e);
            }
        });
    });

    // ── Pickup Point Toggle (now a radio button) ──
    if (togglePickup) {
        togglePickup.addEventListener('change', async () => {
            if (togglePickup.checked) {
                if (box) box.classList.add('show');
                autoDetectUserAddress();
                if (resultsDiv && resultsDiv.children.length === 0) {
                    await performSearch();
                }
            } else {
                if (box) box.classList.remove('show');
                try {
                    await rpc('/shop/monta/select_pickup_point', { shipper_code: false });
                } catch (e) {
                    console.error('Failed to clear pickup point:', e);
                }
            }
        });
    }

    if (btnSearch) {
        btnSearch.addEventListener('click', performSearch);
    }

    [streetInput, houseInput, zipInput, cityInput].forEach(input => {
        if (input) {
            input.addEventListener('keydown', e => {
                if (e.key === 'Enter') { e.preventDefault(); performSearch(); }
            });
        }
    });

    async function performSearch() {
        autoDetectUserAddress();
        const street = streetInput ? streetInput.value.trim() : '';
        const houseNumber = houseInput ? houseInput.value.trim() : '';
        const zip = zipInput ? zipInput.value.trim() : '';
        const city = cityInput ? cityInput.value.trim() : '';
        const country = countrySelect ? countrySelect.value : 'NL';

        if (!zip && !street && !city) {
            showError('Please enter an address or zip/postal code.');
            return;
        }

        hideError();
        showLoading(true);
        if (resultsDiv) resultsDiv.innerHTML = '';

        try {
            const res = await rpc('/shop/monta/get_pickup_points', {
                street, house_number: houseNumber, zip_code: zip, city, country_code: country
            });
            showLoading(false);
            if (res && res.status === 'success') {
                const points = res.pickup_points || [];
                if (points.length === 0) { showError('No pickup points found near this location.'); return; }
                renderPickupPoints(points);
            } else {
                showError((res && res.message) || 'Failed to fetch pickup points.');
            }
        } catch (e) {
            showLoading(false);
            showError('An error occurred while fetching pickup points.');
            console.error(e);
        }
    }

    function renderPickupPoints(points) {
        resultsDiv.innerHTML = '';
        points.forEach(point => {
            const card = document.createElement('div');
            card.className = 'monta-pickup-card d-flex flex-column gap-1';

            let distanceStr = '';
            if (point.distance !== undefined) {
                distanceStr = point.distance >= 1000
                    ? (point.distance / 1000).toFixed(1) + ' km'
                    : Math.round(point.distance) + ' m';
            }

            const priceFormatted = new Intl.NumberFormat('nl-NL', {
                style: 'currency', currency: point.currency || 'EUR'
            }).format(point.price);

            card.innerHTML = `
                <div class="d-flex justify-content-between align-items-start">
                    <div class="monta-pickup-card-title">${escapeHtml(point.company)}</div>
                    <div class="monta-pickup-card-price">${priceFormatted}</div>
                </div>
                <div class="monta-pickup-card-address">
                    ${escapeHtml(point.street)} ${escapeHtml(point.house_number)}<br/>
                    ${escapeHtml(point.postal_code)} ${escapeHtml(point.city)}
                </div>
                <div class="monta-pickup-card-footer">
                    <span class="monta-pickup-card-distance">${distanceStr} away</span>
                    <span class="monta-pickup-card-carrier">${escapeHtml(point.shipper_code)}</span>
                </div>
            `;

            card.addEventListener('click', async () => {
                document.querySelectorAll('.monta-pickup-card').forEach(c => c.classList.remove('active'));
                card.classList.add('active');
                document.querySelectorAll('input.monta-delivery-radio').forEach(r => r.disabled = true);
                showLoading(true);

                try {
                    const res = await rpc('/shop/monta/select_pickup_point', {
                        name: point.company,
                        street: point.street,
                        house_number: point.house_number,
                        zip: point.postal_code,
                        city: point.city,
                        country_code: point.country_code,
                        shipper_code: point.shipper_code,
                        options_json: point.options_json,
                        price: point.price
                    });
                    if (res && res.status === 'success') {
                        window.location.reload();
                    } else {
                        showLoading(false);
                        document.querySelectorAll('input.monta-delivery-radio').forEach(r => r.disabled = false);
                        showError((res && res.message) || 'Failed to select pickup point.');
                    }
                } catch (e) {
                    showLoading(false);
                    document.querySelectorAll('input.monta-delivery-radio').forEach(r => r.disabled = false);
                    showError('An error occurred while selecting the pickup point.');
                    console.error(e);
                }
            });

            resultsDiv.appendChild(card);
        });
    }

    function showLoading(show) {
        if (!loading) return;
        loading.classList.toggle('d-none', !show);
    }

    function showError(msg) {
        if (!errorDiv) return;
        errorDiv.textContent = msg;
        errorDiv.classList.remove('d-none');
    }

    function hideError() {
        if (!errorDiv) return;
        errorDiv.classList.add('d-none');
        errorDiv.textContent = '';
    }

    function escapeHtml(text) {
        if (!text) return '';
        return text.replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initMontaPickup);
} else {
    initMontaPickup();
}

/**
 * CRITICAL: Our custom inputs sit inside Odoo's checkout <form>.
 * Submitting the form with names like "monta_delivery_speed" or "monta_use_pickup"
 * causes Odoo's controller to throw an error, blocking order confirmation.
 * Fix: strip the name attribute from all our inputs just before form submit.
 */
(function preventMontaFieldsFromBeingSubmitted() {
    const MONTA_SELECTORS = [
        'input.monta-delivery-radio',
        '#monta_use_pickup',
        '#monta_pickup_street',
        '#monta_pickup_house',
        '#monta_pickup_zip',
        '#monta_pickup_city',
        '#monta_pickup_country',
    ];

    function stripMontaNames(form) {
        MONTA_SELECTORS.forEach(sel => {
            form.querySelectorAll(sel).forEach(el => {
                el.removeAttribute('name');
                if (el.tagName === 'INPUT' || el.tagName === 'SELECT') {
                    el.disabled = true;
                }
            });
        });
    }

    function attachToForms() {
        document.querySelectorAll('form').forEach(form => {
            if (form._montaSubmitBound) return;
            form._montaSubmitBound = true;
            form.addEventListener('submit', () => {
                stripMontaNames(form);
            }, true); // capture phase — before Odoo's submit handlers
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', attachToForms);
    } else {
        attachToForms();
    }
})();
