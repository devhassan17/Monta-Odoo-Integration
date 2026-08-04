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
    const packagingContainer = document.querySelector('.monta-packaging-container');
    if (!speedContainer && !pickupContainer && !packagingContainer) return;

    // Prevent Odoo checkout card-click bubbling inside our sections
    [speedContainer, pickupContainer, packagingContainer].forEach(el => {
        if (el) el.addEventListener('click', e => e.stopPropagation());
    });

    const deliveryTypeRadios = document.querySelectorAll('input.monta-delivery-radio, input.monta-packaging-radio');
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

    // Helper: auto-detect user's address from checkout DOM form inputs if available
    function autoDetectUserAddress() {
        const domStreet = document.querySelector('input[name="street"], #street');
        const domHouse = document.querySelector('input[name="street2"], #street2');
        const domZip = document.querySelector('input[name="zip"], #zip');
        const domCity = document.querySelector('input[name="city"], #city');

        if (streetInput && domStreet && domStreet.value && !streetInput.value) {
            streetInput.value = domStreet.value.trim();
        }
        if (houseInput && domHouse && domHouse.value && !houseInput.value) {
            houseInput.value = domHouse.value.trim();
        }
        if (zipInput && domZip && domZip.value && !zipInput.value) {
            zipInput.value = domZip.value.trim();
        }
        if (cityInput && domCity && domCity.value && !cityInput.value) {
            cityInput.value = domCity.value.trim();
        }
    }

    autoDetectUserAddress();

    // ── Delivery Speed: radio change handler ──
    // Rows are <label> elements wrapping the radio — clicking is handled natively.
    // We just listen for change to call the backend and sync active classes.
    const deliveryRadios = document.querySelectorAll('input.monta-delivery-radio');
    deliveryRadios.forEach(radio => {
        radio.addEventListener('change', async () => {
            if (!radio.checked) return;
            const selectedType = radio.value;

            // Sync active class on all delivery rows
            document.querySelectorAll('.monta-delivery-speed-container .monta-option-row').forEach(row => {
                row.classList.toggle('monta-option-row--active', row.querySelector('input.monta-delivery-radio') === radio);
            });

            // Uncheck pickup if switching to a standard delivery type
            if (togglePickup && togglePickup.checked) {
                togglePickup.checked = false;
                if (box) box.classList.remove('show');
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

    // ── Packaging: radio change handler ──
    const packagingRadios = document.querySelectorAll('input.monta-packaging-radio');
    packagingRadios.forEach(radio => {
        radio.addEventListener('change', async () => {
            if (!radio.checked) return;
            const selectedVal = radio.value;

            // Sync active class on all packaging rows
            document.querySelectorAll('.monta-packaging-container .monta-option-row').forEach(row => {
                row.classList.toggle('monta-option-row--active', row.querySelector('input.monta-packaging-radio') === radio);
            });

            showLoading(true);
            try {
                const res = await rpc('/shop/monta/select_packaging_type', { packaging_type: selectedVal });
                if (res && res.status === 'success') {
                    window.location.reload();
                } else {
                    showLoading(false);
                }
            } catch (e) {
                showLoading(false);
                console.error('Failed to set packaging type:', e);
            }
        });
    });

    // Section 2: Handle Pickup Point Toggle Checkbox
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
                    togglePickup.disabled = true;
                    showLoading(true);
                    const res = await rpc('/shop/monta/select_pickup_point', {
                        shipper_code: false
                    });
                    if (res && res.status === 'success') {
                        window.location.reload();
                    }
                } catch (e) {
                    console.error("Failed to clear pickup point:", e);
                } finally {
                    togglePickup.disabled = false;
                    showLoading(false);
                }
            }
        });
    }

    if (btnSearch) {
        btnSearch.addEventListener('click', performSearch);
    }

    // Trigger search on Enter key inside zip, street, or city inputs
    [streetInput, houseInput, zipInput, cityInput].forEach(input => {
        if (input) {
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    performSearch();
                }
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
            showError("Please enter an address or zip/postal code.");
            return;
        }

        hideError();
        showLoading(true);
        if (resultsDiv) resultsDiv.innerHTML = '';

        try {
            const res = await rpc('/shop/monta/get_pickup_points', {
                street: street,
                house_number: houseNumber,
                zip_code: zip,
                city: city,
                country_code: country
            });

            showLoading(false);

            if (res && res.status === 'success') {
                const points = res.pickup_points || [];
                if (points.length === 0) {
                    showError("No pickup points found near this location.");
                    return;
                }
                renderPickupPoints(points);
            } else {
                showError((res && res.message) || "Failed to fetch pickup points.");
            }
        } catch (e) {
            showLoading(false);
            showError("An error occurred while fetching pickup points.");
            console.error(e);
        }
    }

    function renderPickupPoints(points) {
        resultsDiv.innerHTML = '';
        points.forEach(point => {
            const card = document.createElement('div');
            card.className = 'monta-pickup-card d-flex flex-column gap-1';
            
            // Format distance (e.g. 1.2 km or 450 m)
            let distanceStr = '';
            if (point.distance !== undefined) {
                if (point.distance >= 1000) {
                    distanceStr = (point.distance / 1000).toFixed(1) + ' km';
                } else {
                    distanceStr = Math.round(point.distance) + ' m';
                }
            }

            // Price formatted
            const priceFormatted = new Intl.NumberFormat('nl-NL', {
                style: 'currency',
                currency: point.currency || 'EUR'
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

            // Handle card selection
            card.addEventListener('click', async () => {
                // Disable all cards and radios to prevent double selection
                document.querySelectorAll('.monta-pickup-card').forEach(c => c.classList.remove('active'));
                card.classList.add('active');
                deliveryTypeRadios.forEach(r => r.disabled = true);
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
                        option_code: point.option_code,
                        point_code: point.code,
                        price: point.price
                    });

                    if (res && res.status === 'success') {
                        // Reload the checkout page so Odoo updates the payment and delivery summary
                        window.location.reload();
                    } else {
                        showLoading(false);
                        deliveryTypeRadios.forEach(r => r.disabled = false);
                        showError((res && res.message) || "Failed to select pickup point.");
                    }
                } catch (e) {
                    showLoading(false);
                    deliveryTypeRadios.forEach(r => r.disabled = false);
                    showError("An error occurred while selecting the pickup point.");
                    console.error(e);
                }
            });

            resultsDiv.appendChild(card);
        });
    }

    function showLoading(show) {
        if (show) {
            loading.classList.remove('d-none');
        } else {
            loading.classList.add('d-none');
        }
    }

    function showError(msg) {
        errorDiv.textContent = msg;
        errorDiv.classList.remove('d-none');
    }

    function hideError() {
        errorDiv.classList.add('d-none');
        errorDiv.textContent = '';
    }

    function escapeHtml(text) {
        if (!text) return '';
        const map = {
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#039;'
        };
        return text.replace(/[&<>"']/g, function(m) { return map[m]; });
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initMontaPickup);
} else {
    initMontaPickup();
}

/**
 * CRITICAL FIX: Our custom radio/checkbox inputs sit inside Odoo's checkout
 * <form>. When the user clicks "Confirm", those field names (monta_delivery_speed,
 * monta_packaging_type, monta_use_pickup) are included in the POST and Odoo's
 * checkout controller rejects the request, blocking order confirmation.
 *
 * Fix: intercept the form submit and clear the `name` attribute from all our
 * custom inputs so they are excluded from the POST body.
 */
(function preventMontaFieldsFromBeingSubmitted() {
    const MONTA_SELECTORS = [
        'input.monta-delivery-radio',
        'input.monta-packaging-radio',
        '#monta_use_pickup',
    ];

    function disableMontaInputsForSubmit(form) {
        MONTA_SELECTORS.forEach(sel => {
            form.querySelectorAll(sel).forEach(el => {
                el.removeAttribute('name');
            });
        });
    }

    function attachToForms() {
        // Odoo's checkout form typically has id="o_website_sale_checkout_main_form"
        // or class "js_website_submit_form". We catch any form on the page.
        document.querySelectorAll('form').forEach(form => {
            if (form._montaSubmitBound) return;   // attach only once
            form._montaSubmitBound = true;
            form.addEventListener('submit', () => {
                disableMontaInputsForSubmit(form);
            }, true);   // capture phase — fires before other submit handlers
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', attachToForms);
    } else {
        attachToForms();
    }
})();
