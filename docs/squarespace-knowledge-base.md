# Squarespace Knowledge Base Reference
# Compiled from Squarespace Help Center (support.squarespace.com)
# For use by Moonraker Agent when automating Squarespace site management
# Last updated: 2026-04-16

---

## 1. Site Architecture

### Versions
- **Version 7.1** (current): Fluid Engine editor, page sections, no template switching needed. All 39 Moonraker SQ clients use 7.1.
- **Version 7.0** (legacy): Template-specific layouts, index pages, sidebar support. Some older clients may still be on 7.0.

### Page Hierarchy
Every Squarespace site is built from: **Pages > Sections > Blocks**
- **Pages**: The top-level content containers (layout pages, collection pages, portfolio pages)
- **Sections**: Vertically-stacked content bands within a page (7.1 only). Each section can have its own background.
- **Blocks**: Individual content elements (text, image, button, code, form, etc.)

---

## 2. Pages Panel

The Pages panel is the primary navigation management interface. It contains:

### Navigation Sections
- **Main Navigation**: Pages listed here appear in the site header menu. Order in the panel = order in the menu.
- **Not Linked**: Pages accessible by direct URL but NOT shown in navigation. Useful for staging pages, landing pages, or pages linked to manually. Still indexed by search engines unless disabled.
- **Secondary Navigation** (7.0 only): Additional menu, usually in footer area.
- **Footer Navigation** (7.0 only): Always displays in site footer.

### Page Panel Actions
- **Add page**: Click `+` icon next to a navigation section
- **Move page**: Click and drag to reorder or move between sections
- **Page settings**: Hover over page, click gear icon (⚙)
- **Delete page**: Hover over page, click trash icon. Recoverable for 30 days.
- **Search pages**: Click search icon (🔍) in top-right of panel. Searches by page title, navigation title, or URL slug.

### Page Types
| Type | Description | When to use |
|---|---|---|
| Layout page (blank) | Empty page for custom content | Service pages, about pages, most content |
| Layout page (pre-built) | Template with placeholder blocks | Quick start for common page types |
| Blog page | Collection of chronological posts | News, articles, insights |
| Store page | Product listings | E-commerce (rare for therapy) |
| Portfolio page (7.1) | Project showcase with sub-pages | Displaying work/case studies |
| Link | External URL in navigation | Booking links, external resources |
| Dropdown (folder) | Groups pages into a dropdown menu | Organizing service pages, location pages |

---

## 3. Dropdowns (Folders)

Dropdowns group pages into a dropdown menu in the navigation.

### Creating a Dropdown
1. Open Pages panel
2. Click `+` next to Main Navigation
3. Select "Dropdown" (or "Folder" in some interfaces)
4. Name the dropdown (this becomes the menu trigger text)
5. Drag existing pages INTO the dropdown, or click "Add Page" inside it

### Dropdown Behavior
- **7.1**: Clicking the dropdown title expands the menu of links
- **7.0**: Behavior varies by template. Some open first page, some expand menu.
- Navigating directly to a dropdown's URL redirects to the first page in the dropdown
- An empty dropdown shows "This folder does not contain any pages"
- Dropdowns collapse on mobile and expand when tapped

### Key Rules
- You can add any page type to a dropdown except index pages
- Pages inside dropdowns still have their own URL slugs
- Dropdown order matches the order in the Pages panel
- You can nest pages but NOT nest dropdowns inside dropdowns

---

## 4. Adding Pages

### Adding a Blank Layout Page (7.1)
1. Open Pages panel
2. Click `+` next to Main Navigation (or inside a Dropdown)
3. Click `+ Add Blank` then click `Page`
4. Enter a page title, press Enter
5. Page opens in editor mode

### Adding a Pre-built Layout Page (7.1)
1. Open Pages panel
2. Click `+`
3. Click a category (About, Contact, Services, etc.)
4. Choose from pre-built layouts with placeholder blocks
5. Edit placeholder content

### Adding a Page to a Dropdown
1. Open Pages panel
2. Click `Add Page` under the dropdown name
3. Or: create page in Main Navigation, then drag it into the dropdown

### Moving Pages Between Sections
- **Main Nav → Not Linked**: Drag page to "Not Linked" section
- **Not Linked → Main Nav**: Drag page from "Not Linked" to desired position
- **Into Dropdown**: Drag page into the dropdown
- **Out of Dropdown**: Drag page out of the dropdown to desired section

---

## 5. Page Settings

Access: Hover over page in Pages panel → click ⚙ (gear icon)

### General Tab
| Setting | Purpose |
|---|---|
| Page Title | Browser tab text, search result title (if no SEO title) |
| Navigation Title | Link text in the navigation menu |
| Page Description | Some 7.0 templates display this on the page |
| URL Slug | The URL path (e.g., /anxiety-therapy) |
| Enable/Disable | Toggle page visibility. Disabled pages return 404. |
| Password | Page-level password protection |

### SEO Tab
| Setting | Purpose |
|---|---|
| SEO Title | Custom title for search results and browser tabs. Overrides page title. |
| SEO Description | Meta description for search results. 50-300 chars recommended, 400 max. |
| Hide from Search Engines | noindex toggle. **CRITICAL: Ensure this is OFF for published pages.** |

### Social Tab
| Setting | Purpose |
|---|---|
| Social Image | Custom OG image for social sharing |
| Social Title | Override title for social sharing |
| Social Description | Override description for social sharing |

### Navigation Tab (7.1 layout pages only)
- Hide header on this page
- Hide footer on this page

### Advanced Tab
| Setting | Purpose |
|---|---|
| Page Header Code Injection | Custom code in `<head>` for this page only |
| Post Blog Item Code Injection | Code injected into every blog post (blog pages only) |

---

## 6. URL Slugs

### Rules
- Must be 3-250 characters (blog/event/product: 3-200)
- Can only contain lowercase letters, numbers, and dashes (-)
- No special characters
- Auto-generated from page title when first created
- Changing the page title does NOT auto-update the slug
- Duplicate slugs: SQ prevents exact duplicates on live pages
- Some slugs are reserved (config, cart, checkout, account, etc.)

### Finding Slugs
- In Pages panel: hover over page → gear → General tab → URL field
- In browser: visit the page while logged out, check address bar
- Full preview mode: click expand arrow to see slug in address bar

### Changing Slugs
- Open page settings → General tab → edit the URL field
- **CRITICAL**: After changing a slug, create a 301 redirect from the old slug
- Deleted page slugs can be reused (except products)

---

## 7. Page Sections (7.1)

Sections are the vertically-stacked content bands that make up a page.

### Section Types
- **Block sections**: Contain blocks arranged via Fluid Engine. Most common.
- **Auto layout sections**: Pre-designed layouts for lists, galleries, etc.
- **Collection page sections**: Auto-generated for blog, store, portfolio pages.
- **Gallery sections**: Image/video galleries with layout options.

### Managing Sections
- **Add section**: Click "Add Section" above or below existing sections
- **Edit section**: Hover → click "Edit Section" for background, padding, colors
- **Duplicate section**: Hover → click duplicate icon
- **Move section**: Hover → click ↑ or ↓ arrows
- **Delete section**: Hover → click trash icon. Cannot be restored after saving.

### Section Styling
Each section can have:
- Background color (from site color palette)
- Background image or video
- Custom padding/spacing
- Width (full-width or content-width)

---

## 8. Blocks

Blocks are the individual content elements placed within sections.

### Key Block Types for Therapy Sites
| Block | Use Case | Notes |
|---|---|---|
| Text | Body copy, headings | Rich text editor with formatting |
| Image | Photos, hero images | Supports alt text, captions, click-through URLs |
| Button | CTAs (Book Now, Contact) | Customizable style, links to any URL |
| Form | Contact forms, intake | Built-in form builder with field types |
| Code | Custom HTML/JS/CSS | For embeds, schema markup, custom widgets |
| Gallery | Image grids/slideshows | Multiple layout options |
| Map | Location embeds | Google Maps integration |
| Accordion | FAQs | Collapsible Q&A sections |
| Spacer | Vertical spacing | Adjustable height (classic editor only, not Fluid Engine) |
| Quote | Testimonials | Styled blockquote |
| Embed | Third-party content | oEmbed standard (YouTube, etc.) |
| Content Link | Internal page links | Visual previews of linked pages |
| Line | Horizontal dividers | Break up content sections |

### Adding Blocks
1. Click "Edit" on the page
2. Click "Add Block" in top-left corner (7.1)
3. Select block type from menu (or search)
4. Block appears with placeholder content
5. Click pencil icon to edit block content

### Block Limits
- No hard limit per page, but recommend max 60 blocks per page
- Too many blocks causes slow page loading, especially on mobile
- In Fluid Engine: no Spacer blocks (use drag positioning instead)

---

## 9. Code Blocks

For deploying custom HTML content to pages.

### Capabilities
- Supports: plain text, HTML, Markdown, CSS (in `<style>` tags)
- JavaScript and iframes: available on Core plan and above (premium feature)
- Can render complex HTML layouts
- Good for: third-party widgets, schema markup, custom embeds, pre-built page content

### Adding a Code Block
1. Edit page → Add Block → search "Code"
2. Click the Code block to add it
3. Click pencil icon to open the code editor
4. Paste HTML/code content
5. Optional: check "Display Source" to show raw code (for documentation)

### Limitations
- Code may not render inside Index pages (7.0)
- Ajax loading can cause issues with custom JavaScript
- Code blocks DON'T benefit from Fluid Engine's responsive grid
- Custom code is not officially supported by Squarespace
- JavaScript in code blocks sometimes disabled while logged in (security measure)

### Troubleshooting
- If code doesn't render: check if page is inside an Index, disable Ajax
- If code blocks block editing: go to Settings > Developer Tools > disable scripts in preview
- Always test in incognito/private browsing (logged-in view may differ)

---

## 10. Code Injection (Site-wide)

For adding tracking scripts, schema markup, and global customizations.

### Access
Settings > Developer Tools > Code Injection (premium feature)

### Injection Points
| Location | When It Loads | Use Case |
|---|---|---|
| Header | In `<head>` on every page | GTM, analytics, custom fonts, global CSS |
| Footer | Before `</body>` on every page | Chat widgets, analytics, deferred JS |
| Lock Page | On password-protected page prompts | Lock page styling |
| Order Confirmation | After purchase (Commerce) | Conversion tracking |

### Per-Page Code Injection
Page settings → Advanced tab:
- **Page Header Code Injection**: `<head>` code for that specific page
- **Post Blog Item Code Injection**: Code injected into every blog post

### Important Notes
- Code injection is a **premium feature** (Core plan+)
- Code injection won't appear on Index landing pages
- We don't recommend HTML in Page Header Code Injection (may not appear if header transparency is off)
- Per-page footer injection is NOT available (only header)

---

## 11. SEO Configuration

### Site-wide SEO
Settings > Marketing > SEO:
- **SEO Site Description**: Default meta description for homepage
- **Title Format**: Pattern for how titles appear in browser tabs
  - Homepage format: `%s` (site title)
  - Page format: `%p — %s` (page title — site title)
  - Item format: `%i — %s` (item title — site title)
  - Supports custom text: `%s | Digital Branding Agency`
- **Hide from Search Engines**: Site-wide noindex. **CRITICAL: Must be OFF.**

### Per-page SEO
Page settings → SEO tab:
- SEO Title (overrides page title in search results)
- SEO Description (meta description, 50-300 chars)
- Hide from Search Engines (noindex per page)

### Keyword Placement Priority
Where keywords affect search ranking:
1. URL page slugs
2. SEO title and page title
3. SEO site/page/item descriptions
4. Headings (H1, H2, H3)
5. Body text
6. Image alt text
7. Image file names
8. Categories and captions

### Built-in SEO Features
- Automatic sitemap.xml generation
- Clean static URLs
- SSL certificates on all sites
- Automatic structured data (WebSite, Organization schemas)
- Automatic `<alt>` tags from image descriptions
- Automatic meta tags from SEO descriptions
- Canonical URLs (automatic, not manually overridable)

---

## 12. URL Redirects

### Access
Settings > Developer Tools > URL Mappings

### Redirect Types
- **301 (Permanent)**: Old page has moved permanently. Transfers SEO rank.
- **302 (Temporary)**: Temporary move. Does not transfer SEO rank.

### Format
```
/old-page -> /new-page 301
/blog/old-post -> /blog/new-post 301
/old-path -> https://external-site.com 302
```

### Rules
- Cannot redirect FROM `/` (homepage is reserved)
- Cannot redirect image or file URLs (CDN-hosted)
- Old URL must NOT exist (delete/disable page first, or change its slug)
- New URL must exist
- Max ~2500 redirect lines (400 KB limit)
- Wildcards NOT supported in standard redirects

---

## 13. Navigation Management

### Adding to Navigation
- Pages added to Main Navigation automatically appear in the site header
- Drag-and-drop to reorder
- Pages in Not Linked are accessible by URL but not in menus

### Creating Dropdown Menus
1. Add a Dropdown to Main Navigation
2. Drag pages into the Dropdown
3. Or create new pages directly inside the Dropdown

### Navigation Links (External)
- Add a Link (not a Page) to navigation
- Can link to: external URLs, email addresses, phone numbers, files, categories, tags
- Set "Open in new tab" for external links

### Navigation Styling
- Fonts, colors, spacing: use Site Styles
- 7.1: Dropdown icons can be customized
- Mobile: navigation collapses to hamburger menu

### Key Behaviors
- Page order in Pages panel = navigation order
- Dropdown title click: 7.1 expands menu, some 7.0 templates navigate to first page
- Pages can be searched by title, navigation title, or URL slug in the panel
- Homepage indicated by house icon

---

## 14. Fluid Engine (7.1 Editor)

The primary content editor for 7.1 sites.

### Features
- Drag-and-drop block positioning on a grid
- Blocks can overlap
- Separate desktop and mobile layouts
- Click and drag block edges to resize
- No Spacer blocks (use positioning instead)

### Adding Content
1. Click "Edit" on the page
2. Click "Add Block" in top-left corner
3. Select block type
4. Drag to position on the grid
5. Resize by dragging edges

### Mobile Editing
- Fluid Engine supports separate mobile layouts
- Blocks can be hidden at specific browser sizes
- Cannot hide blocks from ALL visitors (use page disable/password instead)

### Limits
- Fluid Engine available in all block sections on 7.1
- NOT available in: blog post bodies, event descriptions, non-block areas
- Not available in Squarespace mobile app (edit on computer)

---

## 15. Images and Media

### Uploading Images
- No central media library (images uploaded per-block or per-section)
- 7.1 newer sites may have an Asset Library
- Supported formats: JPG, PNG, GIF, WebP, SVG, ICO
- Max file size: 20MB
- Squarespace auto-generates multiple sizes (100-2500px wide)
- Upload at 2500px wide for best quality

### Image SEO
- Alt text: Set per image block (Design tab or Settings)
- Squarespace auto-adds `<alt>` tags from image descriptions
- Filenames have no SEO impact (SQ renames files internally)
- Images stored on `static1.squarespace.com` CDN

### Image Block Options
- Layout styles: card, collage, inline, overlap, poster, stack
- Fit vs Fill: controls padding around image
- Caption: text below image
- Click-through URL: link destination when clicked
- Focal point: controls responsive cropping center

---

## 16. Permissions and Contributors

### Role Hierarchy
1. **Owner**: Full access, billing, can transfer ownership
2. **Administrator**: All editing + settings (except billing/transfer)
3. **Website Editor**: Content editing, page management, media
4. **Store Manager**: Commerce operations
5. **Email Campaigns Editor**: Marketing email tools
6. **Analytics Contributor**: View-only analytics

### For Moonraker Agent
- `support@moonraker.ai` should have **Administrator** access
- Administrators can: edit pages, manage navigation, access Custom CSS, Code Injection, SEO settings, domains
- Administrators CANNOT: change billing, transfer ownership
- Only owner and administrators can make design changes (fonts, colors, styles)

### Contributor Notifications
- Contributors receive automated emails based on their role
- Can be managed in Notifications tab of contributor profile

---

## 17. Custom CSS

### Access
Pages panel → Custom Code → Custom CSS
Or: Settings > Developer Tools > Custom CSS

### Capabilities
- Applies site-wide
- Changes are live immediately (no publish step)
- Syntax highlighting in the editor
- Supports standard CSS including media queries
- Use `!important` to override built-in styles if needed

### Common Patterns for Therapy Sites
```css
/* Hide announcement bar */
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

## 18. Structured Data (Schema)

### Automatic Schemas
Squarespace automatically generates:
- WebSite schema
- Organization schema (from business info)
- BreadcrumbList (on sub-pages)
- Product schema (on store pages)
- Event schema (on event pages)
- Article schema (on blog posts)

### Custom Schema
- Add via Code Injection (header) for site-wide schema
- Add via per-page Code Injection (header) for page-specific schema
- Or add via Code Block on specific pages

### For Therapy Sites
Recommended schema types to add manually:
- LocalBusiness / MedicalBusiness
- Person (for individual therapists)
- FAQPage (on FAQ pages)
- Service (for specific therapy types)

---

## 19. Squarespace DOM Selectors (for Agent Automation)

### Pages Panel
- Pages panel toggle: `< WEBSITE` link in top-left
- Add page button: `+` next to section headings
- Page settings gear: click/hover reveals ⚙ icon
- Search icon: 🔍 in top-right of Pages panel
- Main Navigation section header
- Not Linked section header

### Page Editor
- Edit button: "EDIT" button in top-left of page preview
- Add Block: "Add Block" button or insert points (`+` icons)
- Add Section: "Add Section" buttons above/below sections
- Save: "Save" button or Ctrl+S
- Exit: "Exit" button, then "Save" to confirm

### Page Settings Dialog
- General tab: page title, nav title, URL slug, enable/disable
- SEO tab: SEO title, SEO description, noindex toggle
- Social tab: social image, title, description
- Navigation tab (7.1): header/footer visibility toggles
- Advanced tab: code injection fields

### Dashboard (account.squarespace.com)
- Site cards: show site name, domain, renewal date
- WEBSITE button: navigates to site config/editor
- DOMAINS button: domain management
- Search box: top-right, filters sites by name/domain
- Pagination: "1-20 of N items" with < > buttons at bottom
- View toggle: grid/list icons near search

---

## 20. Common Gotchas

1. **SQ login rate limiting**: Too many login attempts in quick succession will slow or block the OAuth redirect. Space out login attempts.
2. **React-controlled inputs**: Use `keyboard.type()` not `fill()` for the password field on login.squarespace.com.
3. **Session per site**: After logging in at login.squarespace.com, navigate to account.squarespace.com to access the site selector. Use the Search box to find specific client sites.
4. **Code blocks vs native blocks**: Code blocks don't benefit from Fluid Engine's responsive grid. Native blocks look better on mobile.
5. **URL slug reservations**: Some slugs are reserved (config, cart, checkout, account). The editor will show an error.
6. **Hidden pages still indexed**: Pages in "Not Linked" are still crawled by search engines. To truly hide, disable the page or add noindex.
7. **Code injection is premium**: Requires Core plan or above. On Basic plan, code injection is read-only.
8. **Ajax loading issues**: Custom JavaScript in code blocks may not work with Ajax page loading. Test after deployment.
9. **Image alt text per-block**: There's no bulk alt text management. Each image block needs alt text set individually.
10. **Dropdown URL redirect**: Navigating to a dropdown's URL redirects to its first child page. Empty dropdowns show an error message.
