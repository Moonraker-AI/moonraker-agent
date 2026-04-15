# Squarespace Agent Playbook v1
# Platform: Squarespace (7.0 and 7.1)
# Purpose: Agent reference for page creation, content management, SEO configuration, and navigation
# Last updated: 2026-04-15

---

## 0. Pre-flight

Before starting any Squarespace task, confirm these details from the task payload:

| Field | Example | Required |
|---|---|---|
| `website_url` | `https://www.example.com` | Yes |
| `sq_email` | `support@moonraker.ai` | Yes |
| `sq_password` | (from secure config) | Yes |
| `sq_site_id` | `fox-panda-abc123` (Squarespace subdomain) | Recommended |
| `sq_version` | `7.0` or `7.1` | From scout |

Unlike WordPress, Squarespace has a consistent admin UI across all sites.
The main variable is version (7.0 vs 7.1), which affects the page editor.

---

## 1. Authentication

### Login flow

1. Navigate to `https://login.squarespace.com/`
2. Wait for the login form. Look for:
   - Email input: `input[name="email"]` or `input[type="email"]`
   - Password input: `input[name="password"]` or `input[type="password"]`
3. Squarespace may use a **two-step login**: email first, then password on a second screen
   - Enter email, click Submit/Continue
   - Wait for password field to appear
   - Enter password, click Log In
4. After login, you land on one of:
   - **Site dashboard** (`/config`) if only one site on the account
   - **Site selector** (`app.squarespace.com`) if multiple sites

### Verification

After login, confirm:
- URL contains `/config` or `app.squarespace.com`
- Left sidebar is visible with "Pages", "Design", "Commerce", etc.
- If redirected back to login, credentials may be wrong. Abort and report.

### Moonraker contributor access pattern

For most clients, `support@moonraker.ai` is added as a **Contributor** with Administrator permissions:
- Client invites via Settings > Permissions > Invite Contributor
- Support account accepts via email
- Once accepted, the site appears in the support account's site selector
- This means ONE Squarespace login can manage ALL client sites

### Common obstacles

- **Two-factor authentication (2FA):** If enabled on the support account, abort and report. (The shared support account should NOT have 2FA enabled for agent access.)
- **SSO/Google login:** If the account was created via Google OAuth, we need the actual email/password, not Google sign-in. May need to set a password via "Forgot Password."
- **Account lockout:** Squarespace locks after 10 failed attempts. Wait 30 minutes.
- **Session expiry:** Squarespace sessions last ~2 weeks. The agent gets a fresh login each time.
- **CAPTCHA:** Squarespace occasionally shows hCaptcha on login. If detected, retry once after a 5-second wait. If persistent, abort and report.

---

## 2. Site Navigation (Admin Panel)

### Admin panel URL structure

All admin URLs follow: `{custom-domain}/config/{section}` or `{site-id}.squarespace.com/config/{section}`

| Destination | URL Path |
|---|---|
| Dashboard | `/config` |
| Pages | `/config/pages` |
| Design | `/config/design` |
| Design > Custom CSS | `/config/design/custom-css` |
| Commerce | `/config/commerce` |
| Marketing | `/config/marketing` |
| Analytics | `/config/analytics` |
| Settings | `/config/settings` |
| Settings > SEO | `/config/settings/seo` |
| Settings > Domains | `/config/domains` |
| Settings > Advanced | `/config/settings/advanced` |
| Settings > Code Injection | `/config/settings/advanced/code-injection` |
| Permissions | `/config/settings/permissions` |

### Left sidebar sections

The admin sidebar contains (in order):
1. **Pages** - Main navigation, secondary navigation, unlinked pages
2. **Design** - Template, styles, custom CSS
3. **Commerce** (if enabled) - Products, orders
4. **Marketing** - Email campaigns, SEO, URL redirects
5. **Scheduling** (if enabled)
6. **Analytics** - Traffic, sales
7. **Profiles** - Visitor profiles
8. **Settings** - General, domains, billing, permissions, advanced

---

## 3. Page Management

### Page types in Squarespace

| Type | Description | Navigation |
|---|---|---|
| Standard Page | Blank canvas with sections/blocks | Pages panel > + button |
| Blog Page | Collection of posts | Pages panel > + button |
| Gallery Page | Image/video gallery | Pages panel > + button |
| Events Page | Calendar/event listing | Pages panel > + button |
| Products Page | E-commerce product grid | Pages panel > + button |
| Link | External URL in navigation | Pages panel > + button |
| Folder | Groups pages under a dropdown | Pages panel > + button |
| Index Page (7.0 only) | Stacks multiple pages vertically | Pages panel > + button |

### Creating a new page

1. Navigate to `/config/pages`
2. Click the `+` button in the "Main Navigation" or "Not Linked" section
3. Select page type (usually "Blank" for a standard page)
4. Page opens in editor mode

### Page settings (gear icon)

Access via the gear icon on any page in the Pages panel:

| Setting | Location | Notes |
|---|---|---|
| Page Title | General tab | Used in browser tab |
| URL Slug | General tab | Edit the auto-generated slug |
| Navigation Title | General tab | What appears in the menu |
| Description | SEO tab | Meta description |
| SEO Title | SEO tab | Overrides page title in search |
| Social Image | Social tab | OG image override |
| Password | General tab | Page-level password protection |
| Enable/Disable | Toggle in page list | Hides from navigation but still accessible via URL |

### Page editor (7.1 / Fluid Engine)

The 7.1 editor uses a grid-based system called the **Fluid Engine**:
- Hover over a section to see the `+` button for adding blocks
- Blocks snap to a 24-column grid
- Drag block edges to resize
- Sections are horizontal bands that stack vertically
- Each section can have its own background color/image

### Page editor (7.0)

The 7.0 editor uses a fixed-width block system:
- Click `Edit` on a section
- Click `+` to add a block
- Blocks are fixed-width and stack vertically within columns
- Less flexible than 7.1 but more predictable

### Key block types

| Block | Purpose | Notes |
|---|---|---|
| Text | Rich text content | Most common block |
| Image | Single image | Supports alt text |
| Gallery | Image grid/slideshow | Multiple layout options |
| Button | CTA link | Customizable style |
| Spacer | Vertical spacing | Adjustable height |
| Code | Raw HTML/CSS/JS | For custom embeds |
| Form | Contact forms | Built-in form builder |
| Map | Google Maps embed | Enter address |
| Quote | Styled blockquote | |
| Accordion | Collapsible sections | Good for FAQs |
| Video | YouTube/Vimeo embed | Paste URL |
| Summary | Blog post previews | Links to blog collection |

### Adding content via Code block (most reliable for agent)

For pre-built HTML content, the **Code block** is the most reliable approach:

1. In the editor, click `+` to add a block
2. Search or scroll to "Code"
3. Click the Code block
4. Paste HTML content
5. Check "Display Source" if you want to see raw HTML
6. Click Apply/Save

**Important:** Squarespace Code blocks support HTML, CSS, and JavaScript but:
- No server-side code (PHP, etc.)
- CSS is scoped to the block
- External scripts may be blocked by CSP
- Markdown option is also available in some contexts

---

## 4. Image Management

### Uploading images

Unlike WordPress, Squarespace does NOT have a central media library (until recently in 7.1). Images are uploaded per-block or per-section.

**Method A: Image block**
1. Add an Image block in the editor
2. Click to upload or drag and drop
3. After upload, click the image to edit:
   - Alt text (Design tab)
   - Caption
   - Click-through URL
   - Focal point

**Method B: Section background**
1. Click the section's paintbrush icon
2. Under "Background", click to add image
3. Set focal point for responsive cropping

**Method C: Asset Library (7.1 only, newer feature)**
1. Some 7.1 sites have a built-in asset manager
2. Access via the image selection dialog
3. Previously uploaded images may appear here

### Image best practices

- Squarespace auto-formats images to multiple sizes (100-2500px wide)
- Upload at 2500px wide for best quality
- Supported: JPG, PNG, GIF, WebP, SVG
- Max file size: 20MB
- Alt text is set per-image-block, not centrally
- Filename has no SEO impact (Squarespace renames files internally)

---

## 5. SEO Configuration

### Site-wide SEO settings

Navigate to `/config/settings/seo` (or Marketing > SEO):

| Setting | Purpose |
|---|---|
| Site Title Format | Pattern for browser tab titles |
| SEO Title | Default site title for search |
| SEO Description | Default meta description |
| Search Engine Indexing | noindex toggle (CRITICAL: ensure this is OFF) |

**CRITICAL CHECK:** Verify that "Hide your site from search engines" is NOT checked. New Squarespace sites often have this enabled by default.

### Per-page SEO

Access via page settings (gear icon) > SEO tab:
- **SEO Title**: Custom title tag (overrides page title)
- **SEO Description**: Meta description for this page
- **URL Slug**: Clean, keyword-rich URL path

### Social sharing (OG tags)

Access via page settings > Social tab:
- Social Image: Upload a custom OG image
- Social Title: Override for social sharing
- Social Description: Override for social sharing

Squarespace auto-generates OG tags from page content if not manually set.

### Squarespace SEO limitations vs WordPress

- No granular control over robots meta per page (only site-wide noindex toggle)
- No XML sitemap customization (auto-generated, includes all enabled pages)
- No canonical URL override (uses the page URL automatically)
- No focus keyword tools (no Rank Math/Yoast equivalent)
- Schema markup must be added via Code Injection (not built-in)
- No redirect UI in older plans (available in Business+ via URL Redirects)

---

## 6. Navigation Menu Management

### Main Navigation

The "Main Navigation" section in the Pages panel IS the primary menu:
- Drag pages to reorder
- Drag pages into Folders for dropdown menus
- Pages in "Main Navigation" appear in the site header
- Pages in "Not Linked" are accessible by URL but not in the menu

### Secondary Navigation (Footer)

Some templates support a "Secondary Navigation" section:
- Works the same as Main Navigation
- Appears in the footer (template-dependent)

### Adding a page to navigation

Simply drag it from "Not Linked" to "Main Navigation" in the Pages panel.

### Creating dropdown menus

1. In the Pages panel, click `+` > Folder
2. Name the folder (this becomes the dropdown trigger text)
3. Drag existing pages INTO the folder
4. Or create new pages directly inside the folder

### Navigation display

The navigation rendering is template-controlled:
- Some templates show all top-level pages
- Some limit to a set number and add a hamburger menu
- Mobile navigation is always a hamburger/slide-out menu

---

## 7. Code Injection

### Site-wide code injection

Navigate to `/config/settings/advanced/code-injection`:

| Location | When it loads | Use case |
|---|---|---|
| Header | In `<head>` on every page | Tracking scripts, custom fonts, global CSS |
| Footer | Before `</body>` on every page | Analytics, chat widgets, custom JS |
| Lock Page | On password-protected page prompts | Custom lock page styling |
| Order Confirmation | After purchase (Commerce) | Conversion tracking |

### Per-page code injection

1. Open page settings (gear icon)
2. Go to "Advanced" tab
3. Header and Footer injection fields (per-page)

### Common injections for therapy practices

```html
<!-- Google Analytics (Header) -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-XXXXXXXX"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-XXXXXXXX');
</script>

<!-- Schema markup (Header, per-page) -->
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "MedicalBusiness",
  "name": "Practice Name",
  "address": { ... }
}
</script>

<!-- SimplePractice booking widget (Footer, per-page) -->
<script src="https://widget-cdn.simplepractice.com/assets/integration-1.0.js"></script>
```

---

## 8. URL Redirects

### Access

Navigate to Marketing > URL Redirects (Business plan+)

### Creating redirects

1. Click "Add Redirect"
2. Old URL: Enter the path (e.g., `/old-page`)
3. New URL: Enter destination (e.g., `/new-page` or full external URL)
4. Choose type: 301 (permanent) or 302 (temporary)

### Wildcard redirects

Squarespace supports basic wildcards:
- `/blog/*` redirects all blog URLs
- Useful when restructuring site sections

---

## 9. Custom CSS

### Access

Navigate to Design > Custom CSS (or `/config/design/custom-css`)

### Usage notes

- CSS applies site-wide
- Squarespace uses specific class naming conventions
- Inspect the live site to find the right selectors
- Changes are live immediately (no publish step)
- Code editor supports syntax highlighting

### Common CSS patterns for therapy sites

```css
/* Hide specific elements */
.header-announcement-bar-d { display: none !important; }

/* Custom button styling */
.sqs-block-button-container .sqs-block-button-element {
  border-radius: 8px !important;
  font-weight: 600 !important;
}

/* Responsive adjustments */
@media (max-width: 768px) {
  .page-section .content-wrapper {
    padding: 20px !important;
  }
}
```

---

## 10. Standard Moonraker Deployment Workflow (Squarespace)

1. **Scout** the site first (run sq_scout task)
2. **Log in** to Squarespace admin
3. **Navigate** to the correct site (if multi-site account)
4. **Check for existing page** with same/similar URL slug
5. **Create new page** (or edit existing)
6. **Add content** via Code block (pre-built HTML) or native blocks
7. **Upload images** and set alt text on each
8. **Configure SEO** per-page (title, description, social image)
9. **Set URL slug** to the desired path
10. **Add to navigation** (drag to Main Navigation in Pages panel)
11. **Verify on frontend** (view the live page)
12. **Check mobile rendering** (Squarespace responsive handling)

---

## 11. Error Recovery

- **Editor not loading:** Refresh the page. Squarespace editor is a SPA and can get into bad states.
- **Changes not saving:** Look for a "Save" or "Done" button. Some panels auto-save, others require explicit save.
- **Page not appearing in navigation:** Check that it's in "Main Navigation", not "Not Linked"
- **CSS not applying:** Check specificity. Squarespace's built-in styles may override. Use `!important` if needed.
- **Images not loading:** Check file size (max 20MB) and format. Try re-uploading.
- **Custom code not executing:** Check CSP headers. Squarespace may block inline scripts in Code blocks. Use Code Injection instead.
- **404 after slug change:** Old URL may be cached. Add a redirect from the old slug.

---

## 12. Squarespace vs WordPress: Key Differences for Agents

| Aspect | WordPress | Squarespace |
|---|---|---|
| API access | REST API (full CRUD) | Limited (no content API) |
| Authentication | Application Passwords | Contributor email/password |
| Page editor | Gutenberg blocks or page builder | Fluid Engine (7.1) or blocks (7.0) |
| Media library | Central library | Per-block uploads |
| SEO plugin | Rank Math / Yoast | Built-in (limited) |
| Custom code | Theme files + plugins | Code Injection + Code blocks |
| Navigation | Menu system (Appearance > Menus) | Pages panel drag-and-drop |
| Themes | 10,000+ installable themes | ~100 template families |
| Content injection method | REST API POST preferred | Code block in editor (browser only) |

---

## Appendix A: Squarespace Admin DOM Selectors

### Login page (login.squarespace.com)
- Email: `input[name="email"]`, `input[type="email"]`
- Password: `input[name="password"]`, `input[type="password"]`
- Submit: `button[type="submit"]`, `button[data-test="login-button"]`

### Admin sidebar
- Pages: `a[href*="/config/pages"]`
- Design: `a[href*="/config/design"]`
- Settings: `a[href*="/config/settings"]`
- Analytics: `a[href*="/config/analytics"]`

### Pages panel
- Add page: `button[data-test="add-page"]`, `.add-page-button`
- Page list items: `.navigation-item`, `[class*="PageItem"]`
- Page settings gear: `.gear-icon`, `[class*="settings-icon"]`
- Drag handle: `[class*="drag-handle"]`

### Page editor (7.1)
- Add section: `button[class*="add-section"]`
- Add block: `button[class*="add-block"]`, `.block-inserter`
- Content area: `[class*="section-content"]`
- Save/Done: `button[class*="save"]`, `button[class*="done"]`

### SEO settings
- Site title: `input[name="siteTitle"]`, `[data-test="seo-title"]`
- Description: `textarea[name="siteDescription"]`, `[data-test="seo-description"]`
- Noindex toggle: `[class*="search-engine"]`, `input[type="checkbox"]` near "Hide from search"

---

## Appendix B: Squarespace Version Detection

### 7.0 indicators
- `Static.SQUARESPACE_CONTEXT` in page source
- `sqs-layout` and `sqs-block` classes
- `data-content-field` attributes
- Template-specific body classes (e.g., `brine-template`)

### 7.1 indicators
- `sqs-fluid-engine` or `data-fluid-engine` attributes
- `"templateVersion":"7.1"` in page source
- Fluid Engine grid markup
- No `Static.SQUARESPACE_CONTEXT` (or a different version of it)

### Both versions
- `squarespace.com` references in scripts/styles
- `static1.squarespace.com` CDN URLs
- `sqs-` prefixed class names
- `/config` admin URL pattern

---

## Appendix C: Credential Setup Workflow

### For Moonraker support@ contributor access

1. **During onboarding (IC step "Configure CMS Access"):**
   - Karen or Scott asks the client to add `support@moonraker.ai` as a Contributor
   - Client goes to: Settings > Permissions > Invite Contributor
   - Sets role to **Administrator**
   - Sends invitation

2. **Support account accepts:**
   - Karen checks `support@moonraker.ai` inbox for Squarespace invitation
   - Clicks Accept
   - Site appears in the support account's site selector

3. **Store in Client HQ:**
   - `cms_login_url`: `https://login.squarespace.com`
   - `cms_username`: `support@moonraker.ai`
   - `cms_password`: (shared support account password)
   - `cms_app_password`: not applicable for Squarespace

4. **For agent use:**
   - Agent logs in with support@ credentials
   - Navigates to the correct site via site selector
   - `sq_site_id` helps disambiguate in multi-site account

### Alternative: Direct client credentials

Some clients prefer to share their own login:
- Store their email in `cms_username`
- Store their password in `cms_password`
- Risk: client may change password without telling us
- Recommendation: always prefer contributor access
