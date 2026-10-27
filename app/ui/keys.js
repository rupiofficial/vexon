/* Browser keyboard shortcuts. One source of truth, used in two places:
 *   - browser.html loads this file as a script (shortcuts while the toolbar has focus);
 *   - browser.py injects it into every tab (shortcuts while a page has focus).
 * Only keys that WebView2 does not already handle natively (reload, back/forward, zoom, find,
 * print) are listed here. Returns the command name, or null if the key is not a shortcut. */
function vtKeyCommand(e) {
  if (e.isComposing || !e.key) return null;
  var k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  var ctrl = e.ctrlKey || e.metaKey, alt = e.altKey, shift = e.shiftKey;
  if (ctrl && !alt) {
    if (!shift) {
      if (k === 't') return 'new_tab';
      if (k === 'w' || k === 'F4') return 'close_tab';
      if (k === 'l') return 'focus_address';
      if (k === 'd') return 'bookmark';
      if (k === 'h') return 'history';
      if (k === 'Tab' || k === 'PageDown') return 'next_tab';
      if (k === 'PageUp') return 'prev_tab';
      if (k >= '1' && k <= '9') return 'tab_' + k;
    } else {
      if (k === 't') return 'reopen_tab';
      if (k === 'b') return 'toggle_bookmarks_bar';
      if (k === 'o') return 'bookmarks';
      if (k === 'Tab') return 'prev_tab';
    }
  }
  if (alt && !ctrl && !shift && k === 'd') return 'focus_address';
  if (!ctrl && !alt && !shift && k === 'F6') return 'focus_address';
  return null;
}
