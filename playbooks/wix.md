# Wix Agent Playbook v1
# Platform: Wix (Classic Editor, Wix Studio / Editor X)
# Purpose: Agent reference for page creation, content management, SEO, and navigation
# Last updated: 2026-04-15

---

## 0. Pre-flight

| Field | Example | Required |
|---|---|---|
| `website_url` | `https://www.example.com` | Yes |
| `wix_email` | `support@moonraker.ai` or client email | For admin tasks |
| `wix_password` | (from secure config) | For admin tasks |

Wix has three editor types:
1. **Wix Classic Editor** - drag-and-drop WYSIWYG (most common)
2. **Wix Studio** (formerly Editor X) - responsive design tool
3. **Wix ADI** - AI-built sites (limited editing)

Unlike WordPress, Wix has no useful REST API for content management.
All admin operations require browser automation through the Wix Editor.

---

## 1. Authentication

### Login flow

1. Navigate to `https://users.wix.com/signin`
2. Enter email and password
3. Wix may show Google/Facebook social login options (use email/password)
4. After login, navigate to: `https://manage.wix.com`
5. Select the correct site from the site list
6. Click "Edit Site" to open the Wix Editor

### Moonraker access pattern

For Wix, contributor access works differently than Squarespace:
- Wix has a "Roles & Permissions" system
- Client invites support@moonraker.ai as a "Site Collaborator" with Editor access
- Path: Dashboard > Settings > Roles & Permissions > Invite People
- Collaborator gets full editor access but not billing/domain control

### Common obstacles

- **Two-factor authentication:** If enabled, abort and report
- **Google/Facebook OAuth only:** Some accounts only have social login. May need password reset
- **Account-level CAPTCHA:** Wix shows CAPTCHA after failed login attempts. Wait 15 minutes
- **Wix Editor loading:** The editor is a heavy SPA that can take 10-30 seconds to load

---

## 2. Site Management (Dashboard)

### Dashboard URL structure

| Destination | URL |
|---|---|
| Dashboard | `https://manage.wix.com/dashboard/{site-id}` |
| Site Editor | `https://editor.wix.com/html/editor/web/renderer/edit/{site-id}` |
| SEO Settings | Dashboard > Marketing > SEO |
| Blog Manager | Dashboard > Blog |
| Bookings | Dashboard > Bookings |
| Analytics | Dashboard > Analytics |
| Settings | Dashboard > Settings |

---

## 3. Page Management

### Creating pages

In the Wix Editor:
1. Click "Pages" icon in left toolbar (or press P)
2. Click "+ Add Page"
3. Choose blank or from template
4. Name the page
5. Page opens in editor

### Page settings

Click the `...` menu on any page in the Pages panel:
- **SEO (Google)**: Title tag, meta description, URL slug
- **Social Share**: OG title, description, image
- **Permissions**: Public, Members only, Password protected
- **Duplicate/Delete/Hide**: Page management options

### Adding content

**Text elements:** Click + > Text > choose heading/paragraph style
**Images:** Click + > Image > Upload or from Wix media
**Buttons:** Click + > Button > choose style, set link
**Strips:** Full-width sections with background colors/images
**HTML embed:** Click + > Embed > Custom Element or HTML iframe

### Code block (Custom Element / Velo)

For custom HTML/JS:
1. In Editor: Click + > Embed > Custom Element
2. Or use Velo (formerly Corvid): Dev Mode toggle in top bar
3. Velo allows custom JS on page elements

---

## 4. Image Management

### Wix Media Manager

Unlike Squarespace, Wix has a central media manager:
- Access via the + menu > Image > Wix Media
- Or Dashboard > Media Manager
- Supports: JPG, PNG, GIF, WebP, SVG, video
- Auto-optimizes images for different screen sizes
- Can organize into folders

### Image SEO

- Alt text: Click image in editor > Settings icon > What's in the image?
- Wix auto-generates image URLs on `static.wixstatic.com`
- Filename has no SEO impact (Wix renames internally)

---

## 5. SEO Configuration

### Wix SEO Wiz

Wix has a built-in SEO tool:
- Dashboard > Marketing > SEO > SEO Setup Checklist
- Provides step-by-step optimization guidance
- Sets homepage title, description, and business info

### Per-page SEO

In the Wix Editor:
1. Click Pages panel > select page > `...` menu > SEO (Google)
2. Set: Title tag, Meta description, URL slug
3. Advanced: Custom meta tags, structured data, robots settings

### Wix SEO capabilities vs WordPress/Squarespace

| Feature | WordPress | Squarespace | Wix |
|---|---|---|---|
| Per-page title/desc | Yes (plugin) | Yes | Yes |
| Custom URL slugs | Yes | Yes | Yes |
| Schema markup | Plugin | Code injection | Built-in + custom |
| XML sitemap | Plugin | Auto | Auto |
| Robots meta per page | Plugin | Site-wide only | Per page |
| Canonical URLs | Plugin | Auto | Auto |
| Redirect manager | Plugin | Business plan+ | Built-in |
| Focus keyword | Plugin | No | SEO Wiz |

---

## 6. Navigation

### Wix menu editor

In the Wix Editor:
1. Click on the header/menu area
2. Click "Manage Menu"
3. Add, remove, reorder, and nest menu items
4. Supports dropdown menus via drag-into-folder
5. Can link to pages, sections, external URLs, or anchors

### Menus panel

Alternatively:
1. Click the Pages icon (left toolbar)
2. "Site Menu" section shows the current navigation
3. Drag to reorder, right-click for options

---

## 7. Blog (Wix Blog App)

If installed:
- Dashboard > Blog > Create New Post
- Rich text editor with images, video, categories, tags
- SEO settings per post
- Scheduling and drafts supported
- Blog layout customizable in the Editor

---

## 8. Standard Moonraker Deployment Workflow (Wix)

1. **Scout** the site first (run wix_scout task)
2. **Log in** to Wix via browser
3. **Navigate** to the site dashboard
4. **Open the Editor**
5. **Check for existing page** with same/similar name or URL
6. **Create new page** or edit existing
7. **Add content** (prefer HTML embed for pre-built content)
8. **Upload images** and set alt text
9. **Configure SEO** per-page (title, description, slug)
10. **Add to navigation** via menu manager
11. **Publish** (click Publish button in top-right)
12. **Verify** on frontend

---

## 9. Error Recovery

- **Editor not loading:** Clear browser cache, try incognito. Wix Editor is resource-heavy.
- **Changes not saving:** Look for auto-save indicator. Manual save via Ctrl+S.
- **Page not in menu:** Check Site Menu in Pages panel
- **Published changes not visible:** Wix has aggressive caching. Wait 5 minutes or try incognito.
- **Editor crashed:** Refresh page. Wix auto-saves most changes.

---

## Appendix A: URL Quick Reference

| Destination | URL |
|---|---|
| Login | `https://users.wix.com/signin` |
| Dashboard | `https://manage.wix.com` |
| Editor | `https://editor.wix.com/html/editor/web/renderer/edit/{site-id}` |
| SEO | Dashboard > Marketing > SEO |
| Blog | Dashboard > Blog |
| Media | Dashboard > Media Manager |

## Appendix B: Wix DOM Markers (for scout detection)

| Marker | Meaning |
|---|---|
| `wixstatic.com` | Wix-hosted media |
| `static.parastorage.com` | Wix platform assets |
| `wix-bolt` | Wix rendering engine |
| `thunderbolt` | Newer Wix rendering engine |
| `wix-studio` | Wix Studio site |
| `editorx.com` | Editor X (now Wix Studio) |
| `wixADI` | Wix ADI-built site |
| `siteId` in JSON | Wix internal site identifier |

## Appendix C: Credential Setup Workflow

### For Moonraker collaborator access

1. **During onboarding (IC step "Configure CMS Access"):**
   - Ask client to add `support@moonraker.ai` as a Collaborator
   - Client goes to: Dashboard > Settings > Roles & Permissions > Invite People
   - Role: Editor (full content editing, no billing access)

2. **Support account accepts:**
   - Karen checks support@moonraker.ai inbox for Wix invitation
   - Clicks Accept
   - Site appears in the support account's Wix dashboard

3. **Store in Client HQ:**
   - `cms_login_url`: `https://users.wix.com/signin`
   - `cms_username`: `support@moonraker.ai`
   - `cms_password`: (shared support account password)
   - `cms_app_password`: not applicable for Wix
