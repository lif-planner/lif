// Home Assistant Ingress may not reliably persist add-on cookies. Keep LiF's
// explicit language choice in local storage and carry it through LiF links and
// form actions with a tiny query parameter when running behind ingress.
document.addEventListener("DOMContentLoaded", () => {
    const languageMeta = document.querySelector("meta[name='lif-language']");
    const paramMeta = document.querySelector("meta[name='lif-language-param']");
    const ingressMeta = document.querySelector("meta[name='lif-ingress-path']");
    const ingressPath = ingressMeta ? ingressMeta.content : "";
    if (!languageMeta || !paramMeta || !ingressPath) {
        return;
    }

    const storageKey = "lif-language";
    const paramName = paramMeta.content || "lif_language";
    const currentLanguage = languageMeta.content || "en";
    const currentUrl = new URL(window.location.href);
    const queryLanguage = currentUrl.searchParams.get(paramName);
    let storedLanguage = "";

    try {
        if (queryLanguage) {
            localStorage.setItem(storageKey, queryLanguage);
        }
        storedLanguage = localStorage.getItem(storageKey) || "";
    } catch (error) {
        storedLanguage = queryLanguage || "";
    }

    if (storedLanguage && storedLanguage !== currentLanguage && !queryLanguage) {
        currentUrl.searchParams.set(paramName, storedLanguage);
        window.location.replace(currentUrl.toString());
        return;
    }

    const language = queryLanguage || storedLanguage || currentLanguage;
    if (!language) {
        return;
    }

    const shouldDecorate = (url) => {
        return url.origin === window.location.origin && url.pathname.startsWith(ingressPath);
    };
    const decorateUrl = (value) => {
        if (!value || value.startsWith("#") || value.startsWith("mailto:") || value.startsWith("tel:")) {
            return value;
        }
        const url = new URL(value, window.location.href);
        if (!shouldDecorate(url)) {
            return value;
        }
        url.searchParams.set(paramName, language);
        return url.pathname + url.search + url.hash;
    };

    document.querySelectorAll("a[href]").forEach((link) => {
        link.setAttribute("href", decorateUrl(link.getAttribute("href")));
    });
    document.querySelectorAll("form[action]").forEach((form) => {
        form.setAttribute("action", decorateUrl(form.getAttribute("action")));
        const next = form.querySelector("input[name='next']");
        if (next) {
            next.value = decorateUrl(next.value);
        }
    });
    document.querySelectorAll("select[name='language']").forEach((select) => {
        select.addEventListener("change", () => {
            try {
                localStorage.setItem(storageKey, select.value);
            } catch (error) {
                /* ignore storage being unavailable */
            }
        });
    });
});

// Sidebar accordion: highlight the active link, open only the group it lives in,
// and keep one group open at a time. Groups are default-open in the HTML so the
// nav still works without JS.
document.addEventListener("DOMContentLoaded", () => {
    const nav = document.querySelector(".side-nav");
    const mobileTabbar = document.querySelector(".mobile-tabbar");
    if (!nav && !mobileTabbar) {
        return;
    }

    const path = window.location.pathname;
    // Nav links like "Month view"/"Year view" carry a query string (?goto=...)
    // and point at a fixed index (e.g. /projection/0/audit/) that never
    // literally matches the current page's own index (/projection/5/audit/).
    // Strip the query string and normalize numeric path segments so those
    // still match the page they navigate to.
    const stripQuery = (value) => value.split("?")[0];
    const normalize = (value) => stripQuery(value).replace(/\/\d+(?=\/)/g, "/0");
    let activeGroup = null;
    const markActiveLink = (link) => {
        const href = link.getAttribute("href");
        const hrefPath = stripQuery(href);
        const matches = normalize(href) === normalize(path) || (hrefPath.length > 1 && hrefPath !== "/" && path.startsWith(hrefPath));
        if (matches) {
            link.classList.add("active");
            activeGroup = link.closest("details.side-nav-group") || activeGroup;
        }
    };

    if (nav) {
        nav.querySelectorAll("a[href]").forEach(markActiveLink);
    }
    if (mobileTabbar) {
        mobileTabbar.querySelectorAll("a[href]").forEach(markActiveLink);
    }

    if (!nav) {
        return;
    }

    const groups = Array.from(nav.querySelectorAll("details.side-nav-group"));
    if (!groups.length) {
        return;
    }

    // Collapse everything but the group holding the current page (or the first
    // group, so the nav is never fully blank, e.g. on the dashboard).
    const openGroup = activeGroup || groups[0];
    groups.forEach((group) => {
        group.open = group === openGroup;
    });

    // Accordion: opening one group closes the others.
    groups.forEach((group) => {
        group.addEventListener("toggle", () => {
            if (group.open) {
                groups.forEach((other) => {
                    if (other !== group) {
                        other.open = false;
                    }
                });
            }
        });
    });
});

// Collapsible "Needs attention" rail. State persists across pages.
document.addEventListener("DOMContentLoaded", () => {
    const shell = document.querySelector(".app-shell");
    const toggle = document.querySelector(".rail-toggle");
    const reopen = document.querySelector(".rail-reopen");
    if (!shell || !toggle || !reopen) {
        return;
    }

    const STORAGE_KEY = "lif-rail-collapsed";
    const setCollapsed = (collapsed) => {
        shell.classList.toggle("rail-collapsed", collapsed);
        try {
            localStorage.setItem(STORAGE_KEY, collapsed ? "1" : "0");
        } catch (error) {
            /* ignore storage being unavailable */
        }
    };

    let stored = "0";
    try {
        stored = localStorage.getItem(STORAGE_KEY) || "0";
    } catch (error) {
        stored = "0";
    }
    if (stored === "1") {
        shell.classList.add("rail-collapsed");
    }

    toggle.addEventListener("click", () => setCollapsed(true));
    reopen.addEventListener("click", () => setCollapsed(false));
});

// Mobile off-canvas sidebar drawer behind a hamburger.
document.addEventListener("DOMContentLoaded", () => {
    const shell = document.querySelector(".app-shell");
    const hamburger = document.querySelector(".nav-hamburger");
    const overlay = document.querySelector(".nav-overlay");
    const sidebar = document.querySelector(".sidebar");
    if (!shell || !hamburger || !overlay || !sidebar) {
        return;
    }

    const setOpen = (open) => {
        shell.classList.toggle("nav-open", open);
        hamburger.setAttribute("aria-expanded", open ? "true" : "false");
    };

    hamburger.addEventListener("click", () => setOpen(!shell.classList.contains("nav-open")));
    overlay.addEventListener("click", () => setOpen(false));
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            setOpen(false);
        }
    });
    // Navigating from a drawer link should close the drawer.
    sidebar.querySelectorAll("a").forEach((link) => {
        link.addEventListener("click", () => setOpen(false));
    });
});

// Dropdown action menus (details.nav-group): close whichever one is open when
// the user clicks outside it, or opens a different one, so they don't have to
// click the summary again to dismiss it.
document.addEventListener("DOMContentLoaded", () => {
    const groups = Array.from(document.querySelectorAll("details.nav-group"));
    if (!groups.length) {
        return;
    }

    document.addEventListener("click", (event) => {
        groups.forEach((group) => {
            if (group.open && !group.contains(event.target)) {
                group.open = false;
            }
        });
    });

    groups.forEach((group) => {
        group.addEventListener("toggle", () => {
            if (group.open) {
                groups.forEach((other) => {
                    if (other !== group) {
                        other.open = false;
                    }
                });
            }
        });
    });
});

// Left/right arrow keys step through Previous/Next on month and year audit
// views. Ignored while typing in a form field so the jump-to-month/year
// pickers keep normal arrow-key behavior.
document.addEventListener("DOMContentLoaded", () => {
    const prevLink = document.querySelector("[data-nav='prev']");
    const nextLink = document.querySelector("[data-nav='next']");
    if (!prevLink && !nextLink) {
        return;
    }

    const isEditable = (element) => {
        if (!element) {
            return false;
        }
        const tag = element.tagName;
        return tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA" || element.isContentEditable;
    };

    document.addEventListener("keydown", (event) => {
        if (event.altKey || event.ctrlKey || event.metaKey || isEditable(event.target)) {
            return;
        }
        if (event.key === "ArrowLeft" && prevLink) {
            window.location.href = prevLink.href;
        } else if (event.key === "ArrowRight" && nextLink) {
            window.location.href = nextLink.href;
        }
    });
});
