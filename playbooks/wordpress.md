# WordPress Agent Playbook v1
# Platform: Self-hosted WordPress (wp-admin)
# Purpose: Agent reference for page creation, image upload, SEO configuration, and navigation management
# Last updated: 2026-04-14

---

## 0. Pre-flight

Before starting any WordPress task, confirm these details from the task payload:

| Field | Example | Required |
|---|---|---|
| `wp_admin_url` | `https://example.com/wp-admin` | Yes |
| `wp_username` | `moonraker-agent` | Yes |
| `wp_password` | (from secure config) | Yes |
| `seo_plugin` | `rankmath` or `yoast` or `none` | Yes |
| `theme_type` | `classic` or `block` (FSE) | Yes |
| `editor_type` | `gutenberg` or `classic` or `elementor` | Yes |

These values determine which UI paths to follow. Many WordPress sites differ significantly based on theme type and installed plugins.

---

## 1. Authentication

### Login flow

1. Navigate to `{wp_admin_url}/wp-login.php`
2. Wait for the login form to load. Look for:
   - Input field with `id="user_login"` (username/email)
   - Input field with `id="user_pass"` (password)
   - Button with `id="wp-submit"` (Log In)
3. Fill `user_login` with the username
4. Fill `user_pass` with the password
5. Check the "Remember Me" checkbox (`id="rememberme"`) if present
6. Click `wp-submit`
7. Wait for redirect to the Dashboard (`/wp-admin/`)

### Verification

After login, confirm you are on the Dashboard by checking:
- The URL contains `/wp-admin/` (not `/wp-login.php`)
- The left sidebar menu is visible with items like "Dashboard", "Posts", "Pages", "Media"
- If redirected back to login, credentials may be wrong. Abort and report.

### Common obstacles

- **Two-factor authentication (2FA):** Some sites use plugins like Wordfence or Google Authenticator. If a 2FA screen appears, abort and report.
- **CAPTCHA on login:** If a CAPTCHA challenge appears, abort and report.
- **WAF/Security challenges:** SiteGround (`sg-captcha: challenge`), Cloudflare, Sucuri, or Wordfence may serve JS challenges. The agent IP (204.168.251.129) must be whitelisted in the hosting security settings.
- **Custom login URL:** Plugins like WPS Hide Login change `/wp-login.php` to a custom path. The task payload must include the correct URL.
- **Maintenance mode:** If you see "Briefly unavailable for scheduled maintenance", wait 30 seconds and retry (up to 3 times).

---

## 2. Media Library (Uploading Images)

### Access

Navigate to `{wp_admin_url}/upload.php` or via sidebar: Media > Library

### Upload images

**Method A: Media > Add New (preferred for bulk)**

1. Navigate to `{wp_admin_url}/media-new.php`
2. Click "Select Files" to open file picker
3. Select image file(s)
4. Wait for upload progress to complete

**Method B: Upload within Block Editor**

1. While editing a page, click `+` block inserter
2. Search "Image" and select Image block
3. Click "Upload" or "Media Library"

### Image metadata (critical for SEO)

| Field | Selector | What to set |
|---|---|---|
| Alt Text | Right panel, first field | Descriptive text. Always fill. |
| Title | Right panel, second field | Clean up auto-filled filename |
| Caption | Right panel, third field | Optional |
| Description | Right panel, fourth field | Optional |

### File naming

- Lowercase, hyphen-separated: `anxiety-therapy-session.webp`
- Supported: JPG, PNG, GIF, WebP, ICO, SVG (if plugin installed)

---

## 3. Page Management

### Create a new page

1. Navigate to `{wp_admin_url}/post-new.php?post_type=page`
2. Gutenberg editor opens with title field and content area

### Key block types

| Block | Purpose | Insert via |
|---|---|---|
| Paragraph | Body text | Type directly |
| Heading | H2-H6 | `/heading` |
| Image | Single image | Search "image" |
| Buttons | CTA buttons | Search "buttons" |
| Custom HTML | Raw HTML code | Search "custom html" |
| Columns | Multi-column layout | Search "columns" |

### Pasting HTML content

- Use "Custom HTML" block for pre-built HTML
- Use "Preview" tab to verify rendering
- Gutenberg may auto-convert pasted HTML; Custom HTML block prevents this

---

## 4. Page Attributes

### URL Slug

1. Right sidebar > Post tab > Link section
2. Click URL/slug to edit
3. Use lowercase, hyphens, no spaces

### Featured Image

1. Right sidebar > Post tab > Featured Image
2. Click "Set featured image"
3. Select from library or upload new

### Page Template

1. Right sidebar > Template section
2. Select from available templates (theme-dependent)

### Publishing

1. Click "Publish" (or "Update" for existing)
2. Confirm in the confirmation panel
3. Click "View Page" to verify

---

## 5. SEO Plugin Configuration

### Detecting SEO plugin

Check sidebar for: "Rank Math" (RankMath), "SEO" with Yoast icon (Yoast), "All in One SEO" (AIOSEO)

### RankMath

1. Click Rank Math icon in top-right toolbar
2. Click "Edit Snippet" for SEO fields
3. Set: SEO Title (max 60 chars), Meta Description (max 155 chars), Focus Keyword

### Yoast SEO

1. Click Yoast (Y) icon in top-right toolbar, or scroll below content
2. Set: SEO title, Meta description, Focus keyphrase

### No SEO plugin

Page title = default meta title. No meta description field available.

---

## 6. Navigation Menu Management

### Classic Themes (Appearance > Menus)

1. Navigate to `{wp_admin_url}/nav-menus.php`
2. Pages panel > View All > Check pages > Add to Menu
3. Drag to reorder; drag right for sub-items
4. Check display location in Menu Settings
5. Click "Save Menu"

### Block Themes (Site Editor)

1. Navigate to `{wp_admin_url}/site-editor.php`
2. Click Navigation in left panel
3. Click `+` inside Navigation block to add items
4. Save in top-right corner

---

## 7. Duplicate Detection

Before creating pages, always search for existing pages with same/similar slug:
- Check Pages > All Pages
- Check Trash for deleted pages
- WordPress appends `-2`, `-3` for duplicate slugs

---

## 8. Standard Moonraker Deployment Workflow

1. Upload images (set alt text immediately)
2. Check for existing page (edit if exists)
3. Create/edit page with content
4. Set slug, template, parent page
5. Configure SEO (title, description, focus keyword)
6. Publish and verify
7. Update navigation menu
8. Verify frontend rendering

---

## 9. Error Recovery

- **Editor blank:** Try appending `&classic-editor` to URL
- **Upload fails:** Check file size against limit
- **Menu not visible:** Check menu location assignment
- **Slug not updating:** Go to Settings > Permalinks, click Save
- **Changes not visible:** Clear caching plugin, CDN, try incognito

---

## 10. WordPress REST API Alternative

For bulk operations, the REST API may be more reliable:

| Operation | Method | Endpoint |
|---|---|---|
| List pages | GET | `/wp-json/wp/v2/pages` |
| Create page | POST | `/wp-json/wp/v2/pages` |
| Upload media | POST | `/wp-json/wp/v2/media` |
| Menu items | GET/POST | `/wp-json/wp/v2/menu-items` |

Auth: Application Passwords (WordPress 5.6+) via Basic Auth.

Note: SEO plugin fields and visual page builder content are NOT accessible via REST API.

---

## Appendix A: URL Quick Reference

| Destination | URL |
|---|---|
| Login | `{wp_admin_url}/wp-login.php` |
| Dashboard | `{wp_admin_url}/` |
| All Pages | `{wp_admin_url}/edit.php?post_type=page` |
| Add New Page | `{wp_admin_url}/post-new.php?post_type=page` |
| Media Library | `{wp_admin_url}/upload.php` |
| Menus (classic) | `{wp_admin_url}/nav-menus.php` |
| Site Editor (block) | `{wp_admin_url}/site-editor.php` |
| Plugins | `{wp_admin_url}/plugins.php` |
| Permalinks | `{wp_admin_url}/options-permalink.php` |

## Appendix B: DOM Selectors

### Login page
- Username: `input#user_login`
- Password: `input#user_pass`
- Remember me: `input#rememberme`
- Submit: `input#wp-submit`

### Block Editor (Gutenberg)
- Title: `.editor-post-title__input` or `h1[contenteditable]`
- Block inserter: `.block-editor-inserter button`
- Publish/Update: `.editor-post-publish-button`

### Classic Menus
- Save: `input#save_menu_header`
- Pages panel: `#add-page` accordion
- Add to Menu: `.submit-add-to-menu`

### Media Library
- Search: `input#media-search-input`
- Upload: `.drag-drop-area` or `#plupload-browse-button`

## Appendix C: WAF/Hosting Security Patterns

| Provider | Challenge Type | Whitelist Method |
|---|---|---|
| SiteGround | `sg-captcha: challenge` header, JS challenge | Site Tools > Security > IP Whitelist |
| Cloudflare | JS challenge, Turnstile CAPTCHA | WAF > Custom Rules > Allow agent IP |
| Sucuri | JS challenge page | Sucuri dashboard > Firewall > Whitelist |
| Wordfence | Rate limiting, login lockout | Wordfence > Firewall > Whitelisted IPs |
