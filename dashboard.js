// global-outreach/dashboard.js

// ─── Mode Detection ───────────────────────────────────────────────────────────
// IS_STATIC = true  → GitHub Pages (reads data.json, no Flask needed)
// IS_STATIC = false → local Flask server (uses /api/* endpoints)
const IS_STATIC = (
    window.location.hostname.includes('github.io') ||
    window.location.protocol === 'file:'
);

// Cached no-website CSV string (populated from data.json in static mode)
let _noWebsiteCsv = '';

// Global State
let allLeads  = [];
let configData = {};
let activeTab  = 'overview';
let senderEmail = '';

// Initialize Dashboard
document.addEventListener('DOMContentLoaded', () => {
    // Adjust UI for static (read-only) mode
    if (IS_STATIC) {
        const scrapeBtn = document.querySelector('[onclick="openScrapeModal()"]');
        if (scrapeBtn) {
            scrapeBtn.title = 'Scraper runs automatically via GitHub Actions';
            scrapeBtn.style.opacity = '0.4';
            scrapeBtn.style.cursor  = 'not-allowed';
            scrapeBtn.onclick = (e) => { e.preventDefault(); alert('Scraper runs automatically every day via GitHub Actions.\nCheck the Actions tab on your repo.'); };
        }
        // Swap CSV link to JS-powered Blob download
        const csvLink = document.getElementById('btn-export-csv');
        if (csvLink) {
            csvLink.removeAttribute('href');
            csvLink.onclick = (e) => { e.preventDefault(); downloadCsvBlob(); };
        }
        // Hide the settings save forms (read-only on Pages)
        const settingsBtn = document.getElementById('btn-tab-settings');
        if (settingsBtn) settingsBtn.style.display = 'none';
    }
    fetchData();
});

// Switch Dashboard Tabs
function switchTab(tabId) {
    activeTab = tabId;
    
    // Deactivate all nav buttons and hide tab contents
    document.querySelectorAll('.nav-item').forEach(btn => btn.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach(pane => pane.classList.remove('active'));
    
    // Activate target nav button and show content
    const navBtn = document.getElementById(`btn-tab-${tabId}`);
    if (navBtn) navBtn.classList.add('active');
    
    // FIX #1: removed dead `tabPane` variable that was queried but never used
    const pageTitle    = document.getElementById('page-title');
    const pageSubtitle = document.getElementById('page-subtitle');
    
    if (tabId === 'overview') {
        document.getElementById('tab-overview').classList.add('active');
        pageTitle.innerText    = "Recruitment Agency AI Outreach";
        pageSubtitle.innerText = "Metrics, ATS tech stack breakdown, and campaign status for staffing agencies";
        renderOverview();
    } else if (tabId === 'nowebsite') {
        document.getElementById('tab-nowebsite').classList.add('active');
        pageTitle.innerText = "No-Website Profiles";
        pageSubtitle.innerText = "Leads without websites (downloadable for manual outreach)";
        renderNoWebsiteTable();
    } else if (tabId === 'campaign') {
        document.getElementById('tab-campaign').classList.add('active');
        pageTitle.innerText = "Outreach Queue";
        pageSubtitle.innerText = "Campaign tracker for leads with old/unresponsive websites";
        renderCampaignTable();
    } else if (tabId === 'followup') {
        document.getElementById('tab-followup').classList.add('active');
        pageTitle.innerText = "Follow-Up System";
        pageSubtitle.innerText = "5-day follow-ups · max 20/day · min 30 new outreach always guaranteed";
        renderFollowUpTab();
    } else if (tabId === 'modern') {
        document.getElementById('tab-modern').classList.add('active');
        pageTitle.innerText = "Filtered Modern Sites";
        pageSubtitle.innerText = "Profiles excluded from campaign (website is modern and secure)";
        renderModernTable();
    } else if (tabId === 'replies') {
        document.getElementById('tab-replies').classList.add('active');
        pageTitle.innerText = "Email Replies Inbox";
        pageSubtitle.innerText = "Direct responses received from local medical & dental practices";
        renderReplies();
    } else if (tabId === 'analytics') {
        document.getElementById('tab-analytics').classList.add('active');
        pageTitle.innerText = "Campaign Analytics";
        pageSubtitle.innerText = "Interactive timeline and performance metrics of your campaigns";
        setTimeout(renderAnalyticsChart, 100);
    } else if (tabId === 'settings') {
        document.getElementById('tab-settings').classList.add('active');
        pageTitle.innerText = "Outreach Settings";
        pageSubtitle.innerText = "Configure campaign limits, templates, and promotional endpoints";
        fetchConfig();
    }
}

// ── Follow-Up Tab (Global) ──────────────────────────────────────────────────
function renderFollowUpTab() {
    const now      = Date.now();
    const FIVE_DAY = 5 * 24 * 60 * 60 * 1000;

    const dueLds     = allLeads.filter(l => l.email_status === 'Sent' && !l.followup_sent_at && l.sent_at && (now - new Date(l.sent_at)) >= FIVE_DAY);
    const sentFU     = allLeads.filter(l => l.email_status === 'Follow-Up Sent');
    const pendingLds = allLeads.filter(l => l.email_status === 'Sent' && !l.followup_sent_at && l.sent_at && (now - new Date(l.sent_at)) < FIVE_DAY);

    const setEl = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    setEl('kpi-followup-sent',    sentFU.length);
    setEl('kpi-followup-due',     dueLds.length);
    setEl('kpi-followup-pending', pendingLds.length);
    const badge = document.getElementById('badge-count-followup');
    if (badge) badge.textContent = sentFU.length;

    const combined = [...dueLds, ...sentFU, ...pendingLds];
    const tbody = document.getElementById('body-followup');
    if (!tbody) return;
    if (!combined.length) {
        tbody.innerHTML = `<tr><td colspan="7" class="placeholder-text">No follow-up data yet. Follow-ups appear after 5 days.</td></tr>`;
        return;
    }
    tbody.innerHTML = combined.map(l => {
        const daysSince = l.sent_at ? Math.floor((now - new Date(l.sent_at)) / 86400000) : '—';
        const isDue     = l.email_status === 'Sent' && !l.followup_sent_at && daysSince >= 5;
        const statusHtml = isDue
            ? `<span style="background:rgba(246,173,85,0.15);color:#f6ad55;padding:3px 9px;border-radius:20px;font-size:0.72rem;font-weight:600;">⚡ Due</span>`
            : l.email_status === 'Follow-Up Sent'
            ? `<span style="background:rgba(66,153,225,0.15);color:#4299e1;padding:3px 9px;border-radius:20px;font-size:0.72rem;font-weight:600;">✓ Sent</span>`
            : `<span style="color:#718096;font-size:0.78rem;">⏳ Pending</span>`;
        return `<tr>
            <td>${escHtml(l.name || '—')}</td>
            <td style="font-size:0.75rem;color:#718096">${escHtml(l.email || '—')}</td>
            <td style="color:#718096">${escHtml(l.location || '—')}</td>
            <td style="color:#718096">${l.sent_at ? formatDate(l.sent_at) : '—'}</td>
            <td style="color:#718096">${l.followup_sent_at ? formatDate(l.followup_sent_at) : '—'}</td>
            <td style="font-weight:600;color:${daysSince >= 5 ? '#f6ad55' : '#718096'}">${daysSince}d</td>
            <td>${statusHtml}</td>
        </tr>`;
    }).join('');
}


// Fetch Leads & Stats — auto-detects GitHub Pages vs local Flask
async function fetchData() {
    const refreshIcon = document.getElementById('refresh-icon');
    if (refreshIcon) refreshIcon.classList.add('spinning');
    
    try {
        let stats;

        if (IS_STATIC) {
            // ── GitHub Pages mode: single fetch of data.json ──────────────
            const res = await fetch('data.json?t=' + Date.now()); // cache-bust
            if (!res.ok) throw new Error(`data.json fetch failed: HTTP ${res.status}`);
            const data = await res.json();

            allLeads      = data.leads  || [];
            stats         = data.stats  || {};
            _noWebsiteCsv = data.no_website_csv || '';
            const rawSender = data.sender_email || '';
            const match = rawSender.match(/<([^>]+)>/);
            senderEmail   = match ? match[1].trim() : rawSender.trim();

            // Show last-updated timestamp
            if (data.generated_at) {
                const ts = new Date(data.generated_at);
                const el = document.getElementById('page-subtitle');
                if (el) el.innerText += `  ·  Last updated: ${formatDate(data.generated_at)}`;
            }
        } else {
            // ── Local Flask mode: parallel API calls ──────────────────────
            const [leadsRes, statsRes] = await Promise.all([
                fetch('/api/leads'),
                fetch('/api/stats')
            ]);
            if (!leadsRes.ok || !statsRes.ok) {
                throw new Error(`API error: leads=${leadsRes.status} stats=${statsRes.status}`);
            }
            allLeads = await leadsRes.json();
            stats    = await statsRes.json();
        }

        // Populate the date filter bar
        populateDateFilterBar();

        // Render the filtered view (which computes stats and renders current tab)
        renderCurrentFilteredView();

    } catch (err) {
        console.error('Dashboard data error:', err);
        if (IS_STATIC) {
            alert('Could not load data.json.\nMake sure GitHub Actions has run at least once to generate it.');
        } else {
            alert('Could not connect to local server. Make sure run_dashboard.py is running.');
        }
    } finally {
        if (refreshIcon) {
            setTimeout(() => refreshIcon.classList.remove('spinning'), 600);
        }
    }
}

// Render Overview page widgets
function renderOverview() {
    const listContainer = document.getElementById('recent-leads-list');
    if (!allLeads || allLeads.length === 0) {
        listContainer.innerHTML = '<p class="placeholder-text">No leads scraped yet. Trigger the scraper to start collecting!</p>';
        return;
    }
    
    const filteredLeads = getFilteredLeads();
    if (!filteredLeads || filteredLeads.length === 0) {
        listContainer.innerHTML = '<p class="placeholder-text">No leads scraped on this date.</p>';
        return;
    }
    
    // Get 5 most recent leads
    const recentLeads = filteredLeads.slice(0, 5);
    listContainer.innerHTML = '';
    
    recentLeads.forEach(lead => {
        const item = document.createElement('div');
        item.className = 'recent-lead-item';
        
        let statusTagClass = 'tag-no-web';
        if (lead.status === 'Old Website') statusTagClass = 'tag-old-web';
        if (lead.status === 'No Booking/AI') statusTagClass = 'tag-no-booking';
        if (lead.status === 'Modern Website') statusTagClass = 'tag-modern-web';
        
        item.innerHTML = `
            <div class="lead-info-main">
                <h4>${escapeHtml(lead.name)}</h4>
                <p>${escapeHtml(lead.query)} in ${escapeHtml(lead.location)} | Scraped ${formatDate(lead.scraped_at)}</p>
            </div>
            <span class="lead-status-tag ${statusTagClass}">${escapeHtml(lead.status)}</span>
        `;
        listContainer.appendChild(item);
    });
}

// Render No-Website Table with Filters & Search
function renderNoWebsiteTable() {
    const searchVal = document.getElementById('search-nowebsite').value.toLowerCase().trim();
    const tbody = document.getElementById('body-nowebsite');
    const filteredLeads = getFilteredLeads();
    
    // Filter
    const filtered = filteredLeads.filter(lead => {
        if (lead.status !== 'No Website') return false;
        
        if (searchVal) {
            const nameMatch = lead.name.toLowerCase().includes(searchVal);
            const cityMatch = lead.location.toLowerCase().includes(searchVal);
            const phoneMatch = (lead.phone || '').toLowerCase().includes(searchVal);
            const addressMatch = (lead.address || '').toLowerCase().includes(searchVal);
            return nameMatch || cityMatch || phoneMatch || addressMatch;
        }
        return true;
    });
    
    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="placeholder-text">${searchVal ? 'No matching profiles found.' : 'No profiles in this category.'}</td></tr>`;
        return;
    }
    
    tbody.innerHTML = '';
    filtered.forEach(lead => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><strong>${escapeHtml(lead.name)}</strong></td>
            <td>${escapeHtml(lead.phone || 'N/A')}</td>
            <td>${escapeHtml(lead.address || 'N/A')}</td>
            <td>${escapeHtml(lead.query)}</td>
            <td>${escapeHtml(lead.location)}</td>
            <td>${formatDate(lead.scraped_at)}</td>
        `;
        tbody.appendChild(tr);
    });
}

// Render Campaign Table
function renderCampaignTable() {
    const searchVal = document.getElementById('search-campaign').value.toLowerCase().trim();
    const emailFilter = document.getElementById('filter-email-status').value;
    const tbody = document.getElementById('body-campaign');
    const filteredLeads = getFilteredLeads();
    
    // Filter
    const filtered = filteredLeads.filter(lead => {
        if (lead.status !== 'Old Website' && lead.status !== 'No Booking/AI') return false;
        if (!lead.email || lead.email.trim() === '') return false;
        
        if (emailFilter !== 'ALL') {
            if (lead.email_status !== emailFilter) return false;
        }
        
        if (searchVal) {
            const nameMatch = lead.name.toLowerCase().includes(searchVal);
            const emailMatch = (lead.email || '').toLowerCase().includes(searchVal);
            const cityMatch = lead.location.toLowerCase().includes(searchVal);
            const noteMatch = (lead.website_notes || '').toLowerCase().includes(searchVal);
            return nameMatch || emailMatch || cityMatch || noteMatch;
        }
        return true;
    });
    
    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="placeholder-text">${searchVal || emailFilter !== 'ALL' ? 'No matching campaign profiles found.' : 'No campaigns in queue.'}</td></tr>`;
        return;
    }
    
    tbody.innerHTML = '';
    filtered.forEach(lead => {
        const tr = document.createElement('tr');
        
        let statusTag = 'tag-email-not-sent';
        if (lead.email_status === 'Sent') statusTag = 'tag-email-sent';
        if (lead.email_status === 'Sent (Dry Run)') statusTag = 'tag-email-sent';
        if (lead.email_status === 'Replied') statusTag = 'tag-email-replied';
        if (lead.email_status === 'Failed') statusTag = 'tag-email-failed';
        
        tr.innerHTML = `
            <td><strong>${escapeHtml(lead.name)}</strong><br><span style="font-size: 11px; color: var(--text-muted);">${escapeHtml(lead.location)}</span></td>
            <td><a href="${escapeHtml(lead.website)}" target="_blank" class="truncate">${escapeHtml(lead.website)}</a></td>
            <td><code>${escapeHtml(lead.email)}</code></td>
            <td><span class="lead-status-tag ${statusTag}">${escapeHtml(lead.email_status)}</span></td>
            <td>${lead.sent_at ? formatDate(lead.sent_at) : '—'}</td>
            <td>${lead.email_status === 'Replied' ? '<strong style="color: var(--color-success);">Replied! Check Inbox</strong>' : escapeHtml(lead.website_notes || '')}</td>
        `;
        tbody.appendChild(tr);
    });
}

// Render Filtered Modern Table
function renderModernTable() {
    const searchVal = document.getElementById('search-modern').value.toLowerCase().trim();
    const tbody = document.getElementById('body-modern');
    const filteredLeads = getFilteredLeads();
    
    // Filter
    const filtered = filteredLeads.filter(lead => {
        if (lead.status !== 'Modern Website') return false;
        
        if (searchVal) {
            const nameMatch = lead.name.toLowerCase().includes(searchVal);
            const webMatch = (lead.website || '').toLowerCase().includes(searchVal);
            const cityMatch = lead.location.toLowerCase().includes(searchVal);
            return nameMatch || webMatch || cityMatch;
        }
        return true;
    });
    
    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="6" class="placeholder-text">${searchVal ? 'No matching profiles found.' : 'No profiles in this category.'}</td></tr>`;
        return;
    }
    
    tbody.innerHTML = '';
    filtered.forEach(lead => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><strong>${escapeHtml(lead.name)}</strong></td>
            <td><a href="${escapeHtml(lead.website)}" target="_blank">${escapeHtml(lead.website)}</a></td>
            <td>${lead.website_viewport === 1 ? '<span style="color: var(--color-success);">Responsive (Viewport meta)</span>' : '<span style="color: var(--color-danger);">Non-Responsive</span>'}</td>
            <td>${lead.website_ssl === 1 ? '<span style="color: var(--color-success);">Secure (HTTPS)</span>' : '<span style="color: var(--color-danger);">HTTP Only</span>'}</td>
            <td>${lead.website_copyright_year || 'Unknown'}</td>
            <td>${escapeHtml(lead.location)}</td>
        `;
        tbody.appendChild(tr);
    });
}

// Search Filter Handlers
function filterTable(type) {
    if (type === 'nowebsite') renderNoWebsiteTable();
    if (type === 'campaign') renderCampaignTable();
    if (type === 'modern') renderModernTable();
}

// Fetch config from server for settings page
async function fetchConfig() {
    try {
        const res = await fetch('/api/config');
        if (!res.ok) throw new Error('Failed to load configuration');
        
        configData = await res.json();
        
        // Populate form fields
        document.getElementById('config-limit').value = configData.daily_email_limit;
        document.getElementById('config-promo-dental').value = configData.promo_urls.dental;
        document.getElementById('config-promo-derm').value = configData.promo_urls.derm;
        
        // Load initial templates niche
        loadTemplateNiche();
        
    } catch (err) {
        console.error('Error fetching settings config:', err);
    }
}

// Toggle niche selection inside template editor
// FIX #7: guard against configData being empty (called before fetchConfig resolves)
function loadTemplateNiche() {
    if (!configData || !configData.email_templates) return;
    const niche    = document.getElementById('template-niche').value;
    const template = configData.email_templates[niche];
    if (!template) return;
    document.getElementById('template-subject').value = template.subject || '';
    document.getElementById('template-body').value    = template.body    || '';
}

// Save Config Form
async function saveConfig(e) {
    e.preventDefault();
    const btn = document.getElementById('btn-save-config');
    btn.innerText = "Saving...";
    btn.disabled = true;
    
    configData.daily_email_limit = parseInt(document.getElementById('config-limit').value);
    configData.promo_urls.dental = document.getElementById('config-promo-dental').value.trim();
    configData.promo_urls.derm = document.getElementById('config-promo-derm').value.trim();
    
    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(configData)
        });
        
        if (!res.ok) throw new Error('Save API request failed');
        alert('Configuration settings saved successfully.');
    } catch (err) {
        console.error('Error saving config settings:', err);
        alert('Failed to save configuration settings.');
    } finally {
        btn.innerText = "Save Configuration";
        btn.disabled = false;
    }
}

// Save Templates Form
async function saveTemplates(e) {
    e.preventDefault();
    const btn = document.getElementById('btn-save-templates');
    btn.innerText = "Saving Template...";
    btn.disabled = true;
    
    const niche = document.getElementById('template-niche').value;
    configData.email_templates[niche] = {
        subject: document.getElementById('template-subject').value.trim(),
        body: document.getElementById('template-body').value.trim()
    };
    
    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(configData)
        });
        
        if (!res.ok) throw new Error('Save template request failed');
        alert('Email campaign template saved.');
    } catch (err) {
        console.error('Error saving campaign template:', err);
        alert('Failed to save template.');
    } finally {
        btn.innerText = "Save Template";
        btn.disabled = false;
    }
}

// Modal Controllers
function openScrapeModal() {
    document.getElementById('modal-scrape').classList.add('active');
}

function closeScrapeModal() {
    document.getElementById('modal-scrape').classList.remove('active');
}

// Submit manual scraper background job
async function submitScrapeJob(e) {
    e.preventDefault();
    const submitBtn = document.getElementById('btn-submit-scrape');
    submitBtn.innerText = "Starting Job...";
    submitBtn.disabled = true;
    
    const keyword = document.getElementById('scrape-keyword').value;
    const city = document.getElementById('scrape-city').value.trim();
    const limit = document.getElementById('scrape-limit').value;
    const dryRun = document.getElementById('scrape-dryrun').checked;
    
    try {
        const res = await fetch('/api/trigger-scrape', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                limit: parseInt(limit),
                keyword: keyword || null,
                city: city || null,
                dry_run: dryRun
            })
        });
        
        if (!res.ok) throw new Error('Job execution trigger failed');
        
        const payload = await res.json();
        alert(payload.message || 'Scraper background job started.');
        closeScrapeModal();
        
        // Refresh data in a few seconds to see changes
        setTimeout(fetchData, 4000);
        
    } catch (err) {
        console.error('Error triggering scrape job:', err);
        alert('Failed to trigger background scrape job. Make sure the backend server is running.');
    } finally {
        submitBtn.innerText = "Start Scraper Job";
        submitBtn.disabled = false;
    }
}

// CSV Blob download (used in static / GitHub Pages mode)
function downloadCsvBlob() {
    if (!_noWebsiteCsv) {
        alert('No No-Website leads to export yet.');
        return;
    }
    const blob = new Blob([_noWebsiteCsv], { type: 'text/csv;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = 'no_website_leads.csv';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

// Helper: Escape HTML to prevent injection
function escapeHtml(str) {
    if (!str) return '';
    return str.toString()
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// Helper: Format Date String
function formatDate(isoStr) {
    if (!isoStr) return '—';
    try {
        const date = new Date(isoStr);
        // Simple human readable layout e.g. "Jul 16, 2026, 13:00"
        return date.toLocaleDateString('en-US', {
            month: 'short',
            day: 'numeric',
            year: 'numeric'
        }) + ', ' + date.toLocaleTimeString('en-US', {
            hour: '2-digit',
            minute: '2-digit',
            hour12: false
        });
    } catch (e) {
        return isoStr;
    }
}

// ─── Real-time Date-wise Filter & Stats ───────────────────────────────────────────
let selectedDate = 'ALL';

function getFilteredLeads() {
    if (selectedDate === 'ALL') {
        return allLeads;
    }
    return allLeads.filter(lead => {
        const scrapedDate  = lead.scraped_at ? lead.scraped_at.split('T')[0] : '';
        const sentDate     = lead.sent_at ? lead.sent_at.split('T')[0] : '';
        const followupDate = lead.followup_sent_at ? lead.followup_sent_at.split('T')[0] : '';
        const repliedDate  = lead.replied_at ? lead.replied_at.split('T')[0] : '';
        return (scrapedDate === selectedDate || sentDate === selectedDate || followupDate === selectedDate || repliedDate === selectedDate);
    });
}

function calculateStats(leadsList) {
    const stats = {
        total: 0,
        no_website: 0,
        old_website: 0,
        modern_website: 0,
        sent: 0,
        replied: 0,
        failed: 0,
        conversion_rate: 0
    };

    leadsList.forEach(lead => {
        if (lead.status === 'Filtered (No Email)') return;

        stats.total++;
        if (lead.status === 'No Website') {
            stats.no_website++;
        } else if ((lead.status === 'Old Website' || lead.status === 'No Booking/AI') && lead.email && lead.email.trim() !== '') {
            stats.old_website++;
        } else if (lead.status === 'Modern Website') {
            stats.modern_website++;
        }

        if (lead.email_status === 'Sent' || lead.email_status === 'Sent (Dry Run)' || lead.email_status === 'Replied') {
            stats.sent++;
        }
        if (lead.email_status === 'Replied') {
            stats.replied++;
        } else if (lead.email_status === 'Failed') {
            stats.failed++;
        }
    });

    if (stats.sent > 0) {
        stats.conversion_rate = Math.round((stats.replied / stats.sent) * 100 * 10) / 10;
        if (stats.conversion_rate > 100) stats.conversion_rate = 100;
    }

    return stats;
}

function populateDateFilterBar() {
    const bar = document.getElementById('date-filter-bar');
    if (!bar) return;

    // Extract unique dates from scraped_at, sent_at, followup_sent_at, replied_at
    const dateSet = new Set();
    allLeads.forEach(lead => {
        ['scraped_at', 'sent_at', 'followup_sent_at', 'replied_at'].forEach(field => {
            if (lead[field]) {
                const datePart = lead[field].split('T')[0]; // "YYYY-MM-DD"
                if (datePart && datePart.match(/^\d{4}-\d{2}-\d{2}$/)) {
                    dateSet.add(datePart);
                }
            }
        });
    });

    // Always include today's date if not present
    const todayLocal = new Date();
    const offset = todayLocal.getTimezoneOffset();
    const localDateStr = new Date(todayLocal.getTime() - (offset * 60 * 1000)).toISOString().split('T')[0];
    dateSet.add(localDateStr);

    // Convert to sorted array (newest first)
    const sortedDates = Array.from(dateSet).sort().reverse();

    // Rebuild the HTML
    let html = `<button class="date-tab ${selectedDate === 'ALL' ? 'active' : ''}" onclick="setDateFilter('ALL')">All Time</button>`;
    
    sortedDates.forEach(dateStr => {
        const dateObj = new Date(dateStr);
        let label = '';
        const formattedDate = dateObj.toLocaleDateString('en-US', {
            month: 'short',
            day: 'numeric',
            year: 'numeric'
        });
        
        if (dateStr === localDateStr) {
            label = `Today (${dateObj.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })})`;
        } else {
            label = formattedDate;
        }

        html += `<button class="date-tab ${selectedDate === dateStr ? 'active' : ''}" onclick="setDateFilter('${dateStr}')">${label}</button>`;
    });

    bar.innerHTML = html;
}

function setDateFilter(dateStr) {
    selectedDate = dateStr;
    
    const bar = document.getElementById('date-filter-bar');
    if (bar) {
        bar.querySelectorAll('.date-tab').forEach(btn => {
            btn.classList.remove('active');
        });
        const targetBtn = Array.from(bar.querySelectorAll('.date-tab')).find(btn => {
            return btn.getAttribute('onclick').includes(`'${dateStr}'`);
        });
        if (targetBtn) targetBtn.classList.add('active');
    }

    renderCurrentFilteredView();
}

function renderCurrentFilteredView() {
    const filteredLeads = getFilteredLeads();
    const stats = calculateStats(filteredLeads);
    
    // Update KPI cards
    document.getElementById('kpi-total').innerText      = stats.total      ?? 0;
    document.getElementById('kpi-no-website').innerText = stats.no_website ?? 0;
    document.getElementById('kpi-old-website').innerText = stats.old_website ?? 0;
    document.getElementById('kpi-sent').innerText       = stats.sent       ?? 0;
    document.getElementById('kpi-replied').innerText    = stats.replied    ?? 0;
    document.getElementById('kpi-conversion').innerText = stats.conversion_rate + '%';

    // Update sidebar badges
    document.getElementById('badge-count-nowebsite').innerText = stats.no_website ?? 0;
    document.getElementById('badge-count-campaign').innerText  = stats.old_website ?? 0;
    
    const repliesBadge = document.getElementById('badge-count-replies');
    if (repliesBadge) {
        repliesBadge.innerText = stats.replied ?? 0;
    }

    // Render current tab
    if      (activeTab === 'overview')   renderOverview();
    else if (activeTab === 'nowebsite')  renderNoWebsiteTable();
    else if (activeTab === 'campaign')   renderCampaignTable();
    else if (activeTab === 'modern')     renderModernTable();
    else if (activeTab === 'replies')    renderReplies();
    else if (activeTab === 'analytics')  renderAnalyticsChart();
}

// ─── Real-time Replies rendering & Chart.js Analytics ──────────────────────────────────
let campaignChartInstance = null;

function renderReplies() {
    const searchVal = document.getElementById('search-replies').value.toLowerCase().trim();
    const inbox = document.getElementById('replies-inbox');
    if (!inbox) return;

    const filteredLeads = getFilteredLeads();
    
    // Filter leads that have replied
    const repliedLeads = filteredLeads.filter(lead => {
        if (lead.email_status !== 'Replied') return false;
        
        if (searchVal) {
            const nameMatch = lead.name.toLowerCase().includes(searchVal);
            const emailMatch = (lead.email || '').toLowerCase().includes(searchVal);
            const subjectMatch = (lead.reply_subject || '').toLowerCase().includes(searchVal);
            const bodyMatch = (lead.reply_body || '').toLowerCase().includes(searchVal);
            return nameMatch || emailMatch || subjectMatch || bodyMatch;
        }
        return true;
    });

    if (repliedLeads.length === 0) {
        inbox.innerHTML = `<p class="placeholder-text">${searchVal ? 'No matching replies found.' : 'No replies in inbox yet.'}</p>`;
        return;
    }

    inbox.innerHTML = '';
    repliedLeads.forEach(lead => {
        const card = document.createElement('div');
        card.className = 'reply-card';
        card.onclick = (e) => {
            card.classList.toggle('expanded');
        };

        const subject = lead.reply_subject || '(Subject not captured)';
        const bodyText = lead.reply_body || '(Body content not captured)';
        const queryLower = (lead.query || '').toLowerCase();
        const nicheTag = queryLower.includes('skin') || queryLower.includes('derm') ? 'derm' : 'dental';

        let gmailUrl = '';
        if (senderEmail && senderEmail.trim()) {
            gmailUrl = `https://mail.google.com/mail/u/${encodeURIComponent(senderEmail.trim())}/?view=cm&fs=1&to=${encodeURIComponent(lead.email)}&su=${encodeURIComponent('Re: ' + subject)}`;
        } else {
            gmailUrl = `https://mail.google.com/mail/?view=cm&fs=1&to=${encodeURIComponent(lead.email)}&su=${encodeURIComponent('Re: ' + subject)}`;
        }

        card.innerHTML = `
            <div class="reply-card-header">
                <div class="reply-sender-info">
                    <h3>${escapeHtml(lead.name)}</h3>
                    <p>From: <code>${escapeHtml(lead.email)}</code> | Niche: ${escapeHtml(lead.query)} | Location: ${escapeHtml(lead.location)}</p>
                </div>
                <div class="reply-meta">
                    <span class="reply-niche-tag tag-${nicheTag}">${nicheTag}</span>
                    <span class="reply-date">${lead.replied_at ? formatDate(lead.replied_at) : '—'}</span>
                </div>
            </div>
            <div class="reply-card-subject">${escapeHtml(subject)}</div>
            <div class="reply-card-preview">${escapeHtml(bodyText)}</div>
            <div class="reply-card-actions">
                <button class="reply-expand-btn">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="6 9 12 15 18 9"></polyline>
                    </svg>
                    <span>Toggle Message Body</span>
                </button>
                <a href="${gmailUrl}" target="_blank" class="reply-gmail-btn" onclick="event.stopPropagation();">
                    <svg viewBox="0 0 24 24"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"/></svg>
                    <span>Reply in Gmail</span>
                </a>
            </div>
        `;
        inbox.appendChild(card);
    });
}

function getDailyAnalyticsData() {
    const dailyData = {};
    
    allLeads.forEach(lead => {
        if (!lead.scraped_at) return;
        const dateStr = lead.scraped_at.split('T')[0];
        
        if (!dailyData[dateStr]) {
            dailyData[dateStr] = {
                scraped: 0,
                sent: 0,
                replied: 0,
                no_website: 0
            };
        }
        
        if (lead.status !== 'Filtered (No Email)') {
            dailyData[dateStr].scraped++;
            if (lead.status === 'No Website') {
                dailyData[dateStr].no_website++;
            }
            if (lead.email_status === 'Sent' || lead.email_status === 'Sent (Dry Run)' || lead.email_status === 'Replied') {
                dailyData[dateStr].sent++;
            }
            if (lead.email_status === 'Replied') {
                dailyData[dateStr].replied++;
            }
        }
    });
    
    // Sort dates chronologically
    const sortedDates = Object.keys(dailyData).sort();
    
    return {
        dates: sortedDates,
        scraped: sortedDates.map(d => dailyData[d].scraped),
        sent: sortedDates.map(d => dailyData[d].sent),
        replied: sortedDates.map(d => dailyData[d].replied),
        no_website: sortedDates.map(d => dailyData[d].no_website)
    };
}

function formatDateOnly(dateStr) {
    if (!dateStr) return '—';
    try {
        const parts = dateStr.split('-');
        if (parts.length === 3) {
            const date = new Date(parts[0], parts[1] - 1, parts[2]);
            return date.toLocaleDateString('en-US', {
                month: 'short',
                day: 'numeric',
                year: 'numeric'
            });
        }
        return dateStr;
    } catch (e) {
        return dateStr;
    }
}

function renderAnalyticsChart() {
    const canvas = document.getElementById('campaignLineChart');
    if (!canvas) return;

    const data = getDailyAnalyticsData();

    if (campaignChartInstance) {
        campaignChartInstance.destroy();
    }

    // Try to load Chart.js, if not defined return gracefully
    if (typeof Chart === 'undefined') {
        canvas.parentNode.innerHTML = '<p class="placeholder-text" style="color: var(--color-danger);">Chart.js is not loaded. Make sure you are connected to the internet.</p>';
        return;
    }

    const ctx = canvas.getContext('2d');
    campaignChartInstance = new Chart(ctx, {
        type: 'line',
        data: {
            labels: data.dates.map(d => formatDateOnly(d)),
            datasets: [
                {
                    label: 'Leads Scraped',
                    data: data.scraped,
                    borderColor: '#6366f1',
                    backgroundColor: 'rgba(99, 102, 241, 0.05)',
                    borderWidth: 3,
                    tension: 0.35,
                    fill: true
                },
                {
                    label: 'Emails Sent',
                    data: data.sent,
                    borderColor: '#10b981',
                    backgroundColor: 'rgba(16, 185, 129, 0.05)',
                    borderWidth: 3,
                    tension: 0.35,
                    fill: true
                },
                {
                    label: 'Replies Received',
                    data: data.replied,
                    borderColor: '#ec4899',
                    backgroundColor: 'rgba(236, 72, 153, 0.05)',
                    borderWidth: 3,
                    tension: 0.35,
                    fill: true
                },
                {
                    label: 'No Website Found',
                    data: data.no_website,
                    borderColor: '#f59e0b',
                    backgroundColor: 'rgba(245, 158, 11, 0.05)',
                    borderWidth: 3,
                    tension: 0.35,
                    fill: true
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    labels: {
                        color: '#94a3b8',
                        font: {
                            family: 'Outfit',
                            size: 13
                        }
                    }
                },
                tooltip: {
                    mode: 'index',
                    intersect: false,
                    backgroundColor: '#1e293b',
                    titleColor: '#f8fafc',
                    bodyColor: '#e2e8f0',
                    borderColor: '#334155',
                    borderWidth: 1
                }
            },
            scales: {
                x: {
                    grid: {
                        color: 'rgba(255, 255, 255, 0.05)'
                    },
                    ticks: {
                        color: '#94a3b8',
                        font: {
                            family: 'Inter',
                            size: 12
                        }
                    }
                },
                y: {
                    grid: {
                        color: 'rgba(255, 255, 255, 0.05)'
                    },
                    ticks: {
                        color: '#94a3b8',
                        font: {
                            family: 'Inter',
                            size: 12
                        },
                        precision: 0
                    }
                }
            }
        }
    });
}
