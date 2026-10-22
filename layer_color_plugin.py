import os
from qgis.core import QgsMessageLog, Qgis, QgsProject, QgsLayerTree
from qgis.PyQt.QtWidgets import (
    QMenu,
    QToolTip,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QStyle,
    QApplication,
    QWidgetAction,
    QColorDialog
)
from qgis.PyQt.QtGui import QBrush, QColor, QFontMetrics, QIcon, QPainter, QPalette, QPixmap
from qgis.PyQt.QtCore import (
    Qt,
    QEvent,
    QModelIndex,
    QObject,
    QRect,
    QSettings,
    QSize,
    QTimer,
)

# QAction lives in QtWidgets on Qt5 and in QtGui on Qt6
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction
from qgis.gui import QgsColorWheel

# QSettings key for the "wrap long names" toggle
WRAP_SETTING_KEY = "LayerColorPlugin/wrap_long_names"

# Title of the plugin's entry under the QGIS "Plugins" menu
PLUGIN_MENU_TITLE = "&Layer Color Plugin"

# Label used for the wrap toggle, in both the Plugins menu and the context menu
WRAP_ACTION_LABEL = "Wrap Long Layer Names"

# Characters that long layer names are usually built from. A zero-width space is
# inserted after each one when wrapping, so lines break at these rather than at
# an arbitrary letter.
WRAP_BREAK_CHARS = "_-./\\"

# Text flags used when wrapping names. Plain word wrapping is used together with
# the zero-width breaks inserted by _breakable(), so a name breaks at its own
# separators ("ovezeti_" / "jel_" / "terv_" / "feliratok"). TextWrapAnywhere is
# deliberately not used here: it breaks at an arbitrary letter even when a
# separator is available, which would leave orphans like "felirato" / "k".
WRAP_TEXT_FLAGS = Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft

# Fallback for a name with no separators at all, which word wrapping alone
# cannot break. Used only when a line would otherwise overflow the panel.
WRAP_TEXT_FLAGS_ANYWHERE = WRAP_TEXT_FLAGS | Qt.TextFlag.TextWrapAnywhere


def log_message(message):
    """
    Logs messages to QGIS log panel and Python console.
    """
    QgsMessageLog.logMessage(message, "LayerColorPlugin", Qgis.MessageLevel.Info)
    print(message)


class LayerColorPlugin:
    clipboard_color = None  # Variable to store copied color

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)

    def initGui(self):
        """
        Initializes the plugin by setting the custom delegate and context menu,
        and connects signals for saving/loading project data.
        """
        try:
            # Get the QGIS Layer Tree View
            self.layer_tree_view = self.iface.layerTreeView()
            if not self.layer_tree_view:
                raise Exception("Layer Tree View not available")

            # Remember QGIS's own delegate and context menu policy so unload() can restore them
            self.original_delegate = self.layer_tree_view.itemDelegate()
            self.original_context_menu_policy = self.layer_tree_view.contextMenuPolicy()

            # Set a custom delegate to handle item drawing
            self.delegate = LayerColorDelegate(self, self.layer_tree_view)
            self.layer_tree_view.setItemDelegate(self.delegate)

            # Re-wrap names when the panel is resized. The view does not
            # recompute row heights on its own, so a resize schedules a refresh.
            self.rewrap_timer = QTimer()
            self.rewrap_timer.setSingleShot(True)
            self.rewrap_timer.setInterval(60)
            self.rewrap_timer.timeout.connect(self.refresh_row_heights)
            self.resize_filter = ViewportResizeFilter(self.rewrap_timer, self.delegate,
                                                      self.layer_tree_view)
            self.layer_tree_view.viewport().installEventFilter(self.resize_filter)

            # Add the wrap toggle to the QGIS "Plugins" menu, so the setting can
            # be found without opening the layer tree context menu
            self.wrap_action = QAction(WRAP_ACTION_LABEL, self.iface.mainWindow())
            self.wrap_action.setCheckable(True)
            self.wrap_action.setChecked(bool(self.delegate.wrap_enabled))
            self.wrap_action.setStatusTip(
                "Wrap long layer, group and symbology names over several lines"
            )
            self.wrap_action.toggled.connect(self.toggle_wrap_names)
            self.iface.addPluginToMenu(PLUGIN_MENU_TITLE, self.wrap_action)

            # Set up a custom context menu on the layer tree view
            self.layer_tree_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self.layer_tree_view.customContextMenuRequested.connect(self.show_context_menu)

            # Connect project signals to save and load custom colors
            QgsProject.instance().writeProject.connect(self.save_colors)
            self.iface.projectRead.connect(self.load_colors)

            # If a project is already loaded, restore the colors
            if self.iface.activeLayer():
                self.load_colors()

        except Exception as e:
            log_message(f"Error during plugin initialization: {str(e)}")

    def unload(self):
        """
        Unloads the plugin by disconnecting signals and restoring QGIS's original
        delegate and context menu policy. Highlight colors are kept on the nodes
        so they are not lost if the project is saved while the plugin is disabled.
        Each step runs independently so one failure does not skip the others.
        """
        def attempt(step, func):
            try:
                func()
            except Exception as e:
                log_message(f"Error during unload ({step}): {str(e)}")

        if getattr(self, "wrap_action", None) is not None:
            attempt("plugins menu entry",
                    lambda: self.iface.removePluginMenu(PLUGIN_MENU_TITLE, self.wrap_action))
            self.wrap_action = None

        view = getattr(self, "layer_tree_view", None)
        if view is not None:
            attempt("context menu signal",
                    lambda: view.customContextMenuRequested.disconnect(self.show_context_menu))

            if getattr(self, "resize_filter", None) is not None:
                attempt("resize filter", lambda: view.viewport().removeEventFilter(self.resize_filter))
                self.resize_filter = None

        if getattr(self, "rewrap_timer", None) is not None:
            attempt("rewrap timer", self.rewrap_timer.stop)
            self.rewrap_timer = None

            original_policy = getattr(self, "original_context_menu_policy", None)
            if original_policy is not None:
                attempt("context menu policy", lambda: view.setContextMenuPolicy(original_policy))

            if getattr(self, "delegate", None) is not None:
                original_delegate = getattr(self, "original_delegate", None)
                attempt("delegate", lambda: view.setItemDelegate(
                    original_delegate if original_delegate is not None else QStyledItemDelegate(view)))
                self.delegate.deleteLater()
                self.delegate = None

        attempt("writeProject signal", lambda: QgsProject.instance().writeProject.disconnect(self.save_colors))
        attempt("projectRead signal", lambda: self.iface.projectRead.disconnect(self.load_colors))

        if view is not None:
            attempt("refresh", lambda: view.viewport().update())

    # ------------------------------------------------------------ name wrapping

    def visible_indexes(self):
        """
        Returns the indexes of all currently visible rows (expanded branches
        only), which is what needs new row heights after a re-wrap.
        """
        view = self.layer_tree_view
        model = view.model()
        found = []
        if model is None:
            return found

        def walk(parent):
            for row in range(model.rowCount(parent)):
                index = model.index(row, 0, parent)
                found.append(index)
                if view.isExpanded(index):
                    walk(index)

        walk(QModelIndex())
        return found

    def refresh_row_heights(self):
        """
        Tells the view that row heights may have changed. QTreeView caches them,
        so re-wrapping needs sizeHintChanged for each visible row.
        """
        try:
            delegate = getattr(self, "delegate", None)
            if delegate is None:
                return
            # The drag is over: measure for real again.
            delegate.resizing = False
            delegate.invalidate_caches()
            # One full relayout recomputes every row height. Emitting
            # sizeHintChanged per row instead costs O(rows^2) - with 400 layers
            # that took 17 seconds, against about 40 ms for the relayout.
            try:
                self.layer_tree_view.doItemsLayout()
            except Exception:
                for index in self.visible_indexes():
                    delegate.sizeHintChanged.emit(index)
            self.layer_tree_view.viewport().update()
        except Exception as e:
            log_message(f"Error refreshing row heights: {str(e)}")

    def toggle_wrap_names(self, enabled):
        """
        Turns name wrapping on or off and remembers the choice.
        """
        try:
            delegate = getattr(self, "delegate", None)
            if delegate is None:
                return
            enabled = bool(enabled)
            delegate.wrap_enabled = enabled
            delegate.invalidate_caches()

            # Keep the Plugins menu entry in step when toggled from the layer
            # tree context menu. Signals are blocked so setChecked() does not
            # call straight back into this method.
            action = getattr(self, "wrap_action", None)
            if action is not None and action.isChecked() != enabled:
                blocked = action.blockSignals(True)
                action.setChecked(enabled)
                action.blockSignals(blocked)

            settings = QSettings()
            settings.setValue(WRAP_SETTING_KEY, bool(enabled))
            # Flush now: on Windows QSettings writes to the registry and keeps
            # them cached, so without this the choice can be lost on a crash.
            settings.sync()
            self.refresh_row_heights()
            log_message(f"Wrap long names: {'on' if enabled else 'off'}")
        except Exception as e:
            log_message(f"Error toggling name wrapping: {str(e)}")

    def show_context_menu(self, point):
        """
        Displays the context menu when a user right-clicks in the layer tree view.
        Embeds a color wheel (qgis.gui.QgsColorWheel) with proper lifetime management.
        Also adds an option to open a color picker dialog for an exact color.
        """
        # Temporarily disconnect the signal to avoid recursive calls
        self.layer_tree_view.customContextMenuRequested.disconnect(self.show_context_menu)
        context_menu = QMenu(self.layer_tree_view)

        # Retrieve default QGIS actions if available and add them to the menu
        default_menu_provider = self.layer_tree_view.menuProvider()
        if default_menu_provider:
            default_menu = default_menu_provider.createContextMenu()
            if default_menu:
                context_menu.addActions(default_menu.actions())

        # Create a submenu for layer highlight color actions
        highlight_menu = QMenu("Layer Highlight Color", context_menu)

        # *** New: Add "Pick Exact Color" option at the top ***
        exact_color_action = QAction("Pick Exact Color...", highlight_menu)
        exact_color_action.triggered.connect(lambda: self.open_color_picker_dialog())
        highlight_menu.addAction(exact_color_action)

        # Create an embedded color wheel using QgsColorWheel from QGIS GUI.
        color_wheel = QgsColorWheel(context_menu)
        # If a node is selected and already has a highlight color, initialize the wheel with that color.
        selected_nodes = self.layer_tree_view.selectedNodes()
        if selected_nodes:
            current_color = selected_nodes[0].customProperty("highlight_color", None)
            if current_color:
                color_wheel.setColor(QColor(current_color))
        # Connect the color change signal to update the node's color
        color_wheel.colorChanged.connect(self.set_layer_color_from_widget)
        # Embed the color wheel into the submenu via a QWidgetAction
        color_action = QWidgetAction(highlight_menu)
        color_action.setDefaultWidget(color_wheel)
        highlight_menu.addAction(color_action)

        # Add additional actions for remove, copy, and paste color
        remove_color_action = QAction("Remove Background Color", highlight_menu)
        remove_color_action.triggered.connect(lambda: self.handle_remove_color(context_menu))
        highlight_menu.addAction(remove_color_action)

        copy_color_action = QAction("Copy Highlight Color", highlight_menu)
        copy_color_action.triggered.connect(lambda: self.handle_copy_color(context_menu))
        highlight_menu.addAction(copy_color_action)

        paste_color_action = QAction("Paste Highlight Color", highlight_menu)
        paste_color_action.triggered.connect(lambda: self.handle_paste_color(context_menu))
        highlight_menu.addAction(paste_color_action)

        # Toggle for wrapping long layer/group names over several lines
        highlight_menu.addSeparator()
        wrap_action = QAction(WRAP_ACTION_LABEL, highlight_menu)
        wrap_action.setCheckable(True)
        wrap_action.setChecked(bool(getattr(self.delegate, "wrap_enabled", False)))
        wrap_action.toggled.connect(self.toggle_wrap_names)
        highlight_menu.addAction(wrap_action)

        # Insert the custom highlight menu before the last valid action (normally "Properties...").
        # In QGIS 4 / Qt6 the default context menu may contain null or invalid QAction entries;
        # inserting before one of those can trigger warnings or instability, so skip them.
        inserted = False
        for action in reversed(context_menu.actions()):
            if action is None:
                continue
            try:
                if action.text():
                    context_menu.insertMenu(action, highlight_menu)
                    inserted = True
                    break
            except RuntimeError:
                continue
        if not inserted:
            context_menu.addMenu(highlight_menu)

        # Define a cleanup function to disconnect and delete the color wheel widget
        def cleanup():
            try:
                color_wheel.colorChanged.disconnect(self.set_layer_color_from_widget)
            except Exception as e:
                log_message(f"Error disconnecting color wheel signal: {str(e)}")
            # Remove the widget from the action and schedule its deletion
            color_action.setDefaultWidget(None)
            color_wheel.deleteLater()
            # Reconnect the context menu signal
            self.layer_tree_view.customContextMenuRequested.connect(self.show_context_menu)

        # Connect the cleanup function to the menu's aboutToHide signal
        context_menu.aboutToHide.connect(cleanup)

        # Execute the context menu at the global position corresponding to the click point
        context_menu.exec(self.layer_tree_view.mapToGlobal(point))

    def open_color_picker_dialog(self):
        """
        Opens a modal color picker dialog for the user to select an exact color.
        The selected color is applied to all selected nodes.
        """
        selected_nodes = self.layer_tree_view.selectedNodes()
        initial_color = None
        if selected_nodes:
            current_color = selected_nodes[0].customProperty("highlight_color", None)
            if current_color:
                initial_color = QColor(current_color)
        # Create the color picker dialog; parent it to the layer tree view
        dialog = QColorDialog(initial_color if initial_color else QColor(), self.layer_tree_view)
        dialog.setOption(QColorDialog.ColorDialogOption.ShowAlphaChannel, False)
        # Execute the dialog modally
        if dialog.exec():
            color = dialog.selectedColor()
            if color.isValid():
                self.set_layer_color_from_widget(color)

    def set_layer_color_from_widget(self, color):
        """
        Called when the user changes the color in the embedded color wheel or via the color picker.
        Updates all selected nodes with the new highlight color.
        """
        selected_nodes = self.layer_tree_view.selectedNodes()
        if not selected_nodes:
            log_message("No layer or group selected to set the color.")
            return
        for node in selected_nodes:
            node.setCustomProperty("highlight_color", color.name())
            name = node.layerId() if QgsLayerTree.isLayer(node) else node.name()
            log_message(f"Layer or group: {name}, color set: {color.name()}")
        self.layer_tree_view.viewport().update()

    def handle_remove_color(self, context_menu):
        """
        Handles the action to remove the highlight color from the selected nodes.
        """
        self.remove_layer_color()
        context_menu.close()

    def handle_copy_color(self, menu):
        """
        Handles the action to copy the highlight color from the first selected node.
        """
        self.copy_highlight_color()
        menu.close()

    def handle_paste_color(self, menu):
        """
        Handles the action to paste the copied highlight color onto the selected nodes.
        """
        self.paste_highlight_color()
        menu.close()

    def copy_highlight_color(self):
        """
        Copies the highlight color from the first selected node into a clipboard variable.
        """
        selected_nodes = self.layer_tree_view.selectedNodes()
        if not selected_nodes:
            log_message("No layer or group selected to copy the color.")
            return

        node = selected_nodes[0]
        color = node.customProperty('highlight_color', None)
        if color:
            LayerColorPlugin.clipboard_color = color
            log_message(f"Color '{color}' copied to clipboard.")
        else:
            log_message("No color assigned to the selected item.")

    def paste_highlight_color(self):
        """
        Pastes the copied highlight color onto all selected nodes.
        """
        if not LayerColorPlugin.clipboard_color:
            log_message("No color available on the clipboard to paste.")
            return

        selected_nodes = self.layer_tree_view.selectedNodes()
        if not selected_nodes:
            log_message("No layer or group selected to paste the color.")
            return

        for node in selected_nodes:
            node.setCustomProperty("highlight_color", LayerColorPlugin.clipboard_color)
            name = node.layerId() if QgsLayerTree.isLayer(node) else node.name()
            log_message(f"Color '{LayerColorPlugin.clipboard_color}' pasted in item '{name}'.")
        self.layer_tree_view.viewport().update()

    def set_layer_color(self):
        """
        Placeholder method for setting layer color, no longer used due to the embedded color wheel.
        """
        pass

    def remove_layer_color(self):
        """
        Removes the highlight color from all selected nodes.
        """
        selected_nodes = self.layer_tree_view.selectedNodes()
        if not selected_nodes:
            return

        for layer in selected_nodes:
            if hasattr(layer, "customProperty"):
                color = layer.customProperty("highlight_color", None)
                if color:
                    layer.removeCustomProperty("highlight_color")
                    name = layer.layerId() if QgsLayerTree.isLayer(layer) else layer.name()
                    log_message(f"Color removed from layer or group: {name}")
        self.layer_tree_view.viewport().update()

    def calculate_contrast_ratio(self, background_color):
        """
        Calculates the contrast ratio between the background color and black text.
        """
        bg_color = QColor(background_color)

        def get_luminance(r, g, b):
            rs = r / 255.0
            gs = g / 255.0
            bs = b / 255.0
            rs = rs / 12.92 if rs <= 0.03928 else ((rs + 0.055) / 1.055) ** 2.4
            gs = gs / 12.92 if gs <= 0.03928 else ((gs + 0.055) / 1.055) ** 2.4
            bs = bs / 12.92 if bs <= 0.03928 else ((bs + 0.055) / 1.055) ** 2.4
            return 0.2126 * rs + 0.7152 * gs + 0.0722 * bs

        bg_luminance = get_luminance(bg_color.red(), bg_color.green(), bg_color.blue())
        text_luminance = get_luminance(0, 0, 0)  # Black text luminance
        lighter = max(bg_luminance, text_luminance)
        darker = min(bg_luminance, text_luminance)
        return (lighter + 0.05) / (darker + 0.05)

    def save_colors(self):
        """
        Recursively saves the highlight color for each node in the project.
        """
        try:
            def save_node_colors(node):
                if hasattr(node, "customProperty"):
                    color = node.customProperty("highlight_color", None)
                    if color:
                        node.setCustomProperty("highlight_color", color)
                        name = node.layerId() if QgsLayerTree.isLayer(node) else node.name()
                        log_message(f"Saving color '{color}' for '{name}'")
                for child in node.children():
                    save_node_colors(child)

            root = self.iface.layerTreeView().layerTreeModel().rootGroup()
            save_node_colors(root)
            log_message("All colors saved successfully.")
        except Exception as e:
            log_message(f"Error saving colors: {str(e)}")

    def load_colors(self):
        """
        Recursively loads and applies the saved highlight colors for each node in the project.
        """
        try:
            def load_node_colors(node):
                if hasattr(node, "customProperty"):
                    color = node.customProperty("highlight_color")
                    if color:
                        name = node.layerId() if QgsLayerTree.isLayer(node) else node.name()
                        log_message(f"Loaded color '{color}' for '{name}'")
                for child in node.children():
                    load_node_colors(child)

            root = self.iface.layerTreeView().layerTreeModel().rootGroup()
            load_node_colors(root)
            # The tree is a different one now, so cached per-row measurements
            # and layer lookups no longer apply.
            if getattr(self, "delegate", None) is not None:
                self.delegate.invalidate_caches(drop_heights=True)
            self.layer_tree_view.viewport().update()
            log_message("All colors restored successfully.")
        except Exception as e:
            log_message(f"Error loading colors: {str(e)}")


class ViewportResizeFilter(QObject):
    """
    Watches the layer tree viewport for resizes and schedules a re-wrap.

    Exceptions are swallowed on purpose: an exception escaping a Python event
    filter can propagate into Qt and, on Qt6, take QGIS down with it.
    """

    def __init__(self, timer, delegate=None, parent=None):
        super().__init__(parent)
        self.timer = timer
        self.delegate = delegate
        self.last_width = None

    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.Type.Resize:
                width = event.size().width()
                # Only react to width changes; height changes cannot re-wrap
                if width != self.last_width:
                    self.last_width = width
                    # Mark the drag as in progress so rows are not re-measured
                    # at every intermediate width; the timer re-wraps them once
                    # the drag stops.
                    if self.delegate is not None:
                        self.delegate.resizing = True
                    if self.timer is not None:
                        self.timer.start()
        except Exception as e:
            log_message(f"Error in viewport resize filter: {str(e)}")
        return False


class LayerColorDelegate(QStyledItemDelegate):
    def __init__(self, layer_colors, parent=None):
        """
        Initializes the custom delegate for rendering layer tree items,
        and loads custom icons from the plugin's icon directory.
        """
        super().__init__(parent)
        self.layer_color_plugin = layer_colors
        plugin_dir = self.layer_color_plugin.plugin_dir
        icons_dir = os.path.join(plugin_dir, "icon")
        self.default_memory_icon = QIcon(os.path.join(icons_dir, "mIndicatorMemory.svg"))
        self.default_virtual_icon = QIcon(os.path.join(icons_dir, "mIconVirtualLayer.svg"))
        self.default_missing_icon = QIcon(os.path.join(icons_dir, "mIndicatorBadLayer.svg"))
        self.default_nocrs_icon = QIcon(os.path.join(icons_dir, "mIndicatorNoCRS.svg"))

        # Word wrapping of long layer/group names (off by default)
        self.wrap_enabled = QSettings().value(WRAP_SETTING_KEY, False, type=bool)

        # Caches. sizeHint() and paint() run for every row on every relayout,
        # and QGIS relayouts the whole tree on each resize step, so the costly
        # parts (asking the style where text starts, measuring wrapped text,
        # inspecting a layer's provider) are remembered rather than redone.
        self._offset_cache = {}   # (depth, line height) -> text left offset
        self._measure_cache = {}  # (text, width) -> (height, flags)
        self._icon_cache = {}     # row key -> fallback icon
        self._row_heights = {}    # row key -> height from the last full layout
        self._cache_generation = 0

        # While the panel is being dragged, rows keep the heights they already
        # have instead of being re-measured at every intermediate width. The
        # names are re-wrapped once, when the drag stops.
        self.resizing = False

    def invalidate_caches(self, drop_heights=False):
        """
        Drops cached layout measurements. Called when something that feeds them
        changes: the wrap setting, the panel width, or the project contents.

        Row heights are kept by default: they are what a drag reuses instead of
        re-measuring, and the relayout that follows overwrites them anyway.
        Pass drop_heights for a different project, where the rows are not the
        same rows at all.
        """
        self._offset_cache.clear()
        self._measure_cache.clear()
        self._icon_cache.clear()
        if drop_heights:
            self._row_heights.clear()
        self._cache_generation += 1

    @staticmethod
    def _index_key(index):
        """A hashable identity for a row, stable while the tree is unchanged."""
        key = []
        while index.isValid():
            key.append(index.row())
            index = index.parent()
        return tuple(key)

    # ---------------------------------------------------------------- wrapping

    def _node_for(self, index):
        """
        Returns the layer tree node for an index, or None for legend/symbology
        rows (those are left exactly as QGIS draws them).
        """
        try:
            return self.layer_color_plugin.layer_tree_view.index2node(index)
        except Exception:
            return None

    def _indentation(self, index):
        """
        Horizontal space the tree view uses for the expand arrows of this row.
        """
        view = self.layer_color_plugin.layer_tree_view
        depth = 0
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        return view.indentation() * (depth + 1)

    # ------------------------------------------------------------- indicators

    def _indicators(self, index):
        """
        QGIS's own layer tree indicators for this row (filter, temporary layer,
        embedded, notes, no CRS, broken layer...). QGIS paints these from its
        own item delegate, which this plugin replaces, so they are drawn here
        instead. Returns an empty list for symbology rows.
        """
        node = self._node_for(index)
        if node is None:
            return []
        try:
            return list(self.layer_color_plugin.layer_tree_view.indicators(node) or [])
        except Exception:
            return []

    def _fallback_icon(self, index):
        """
        Icon used only when QGIS reports no indicators for a layer, so the row
        still shows that it is in-memory, virtual, broken or missing a CRS.
        """
        node = self._node_for(index)
        if node is None or not QgsLayerTree.isLayer(node):
            return None
        # Cached: inspecting the layer's validity, CRS and provider costs about
        # 30 microseconds, and this runs per row on every relayout. The cache is
        # dropped whenever row heights are refreshed or a project is loaded.
        cache_key = self._index_key(index)
        cached = self._icon_cache.get(cache_key)
        if cached is not None and cached[0] == self._cache_generation:
            return cached[1]
        # Guarded: this runs from sizeHint() and paint(), and an exception
        # escaping into Qt's layout or painting code crashes QGIS outright.
        icon = None
        try:
            layer = node.layer()
            if layer is None or not layer.isValid():
                icon = self.default_missing_icon
            elif not layer.crs().isValid() and layer.isSpatial():
                icon = self.default_nocrs_icon
            else:
                provider = layer.dataProvider()
                provider_name = provider.name() if provider else ""
                if provider_name == "memory":
                    icon = self.default_memory_icon
                elif provider_name == "virtual":
                    icon = self.default_virtual_icon
        except Exception as e:
            log_message(f"Error choosing status icon: {str(e)}")
            return None
        if len(self._icon_cache) > 8000:
            self._icon_cache.clear()
        self._icon_cache[cache_key] = (self._cache_generation, icon)
        return icon

    def _right_items(self, index, line_height, row_top, viewport_width):
        """
        Lays out what is drawn at the right edge of a row and returns a list of
        (indicator_or_None, icon, rect), left to right.

        The strip is right-aligned against the viewport the way QGIS does it,
        leaving room for the layer mark, so the icons stay where QGIS expects
        them. Icons are squares of one line's height, so a wrapped (taller) row
        does not scale them up.
        """
        view = self.layer_color_plugin.layer_tree_view
        side = max(8, line_height)
        spacing = max(1, side // 10)
        try:
            mark_width = view.layerMarkWidth()
        except Exception:
            mark_width = 0

        # Guarded as a whole: callers run inside Qt's layout and painting, where
        # an escaping exception brings QGIS down rather than raising in Python.
        try:
            entries = [(ind, ind.icon()) for ind in self._indicators(index)]
            if not entries:
                fallback = self._fallback_icon(index)
                if fallback is not None:
                    entries = [(None, fallback)]
        except Exception as e:
            log_message(f"Error collecting layer indicators: {str(e)}")
            return []
        if not entries:
            return []

        top = row_top + max(0, (line_height - side) // 2)
        right = viewport_width - spacing - mark_width
        items = []
        for position, (indicator, icon) in enumerate(reversed(entries)):
            left = right - side * (position + 1)
            items.append((indicator, icon, QRect(left, top, side, side)))
        items.reverse()
        return items

    def _reserved_right(self, index, line_height):
        """
        Width kept free on the right so wrapped text does not run under the
        indicator strip.
        """
        view = self.layer_color_plugin.layer_tree_view
        try:
            viewport_width = view.viewport().width()
        except Exception:
            return 0
        items = self._right_items(index, line_height, 0, viewport_width)
        if not items:
            # Layer rows still reserve room, so the layout does not jump when a
            # layer becomes invalid and gains an icon.
            node = self._node_for(index)
            if node is not None and QgsLayerTree.isLayer(node):
                return line_height + 6
            return 0
        return max(0, viewport_width - items[0][2].left() + 4)

    def _wrap_width(self, option, index, line_height):
        """
        Width available for the name text: the visible panel width, minus the
        tree indentation, the item's own icon/checkbox, and the status icon.
        The viewport width is used (not option.rect) because QGIS sizes the
        layer tree column to its widest item, so option.rect can be far wider
        than what the user actually sees.
        """
        total = self._viewport_width(option)
        usable = (
            total
            - self._indentation(index)
            - self._text_left_offset(option, index, line_height)
            - self._reserved_right(index, line_height)
        )
        return max(40, usable)

    def _viewport_width(self, option):
        """Width of the visible panel, whatever option was handed to us."""
        if option.widget:
            return option.widget.viewport().width()
        view = self.layer_color_plugin.layer_tree_view
        if view is not None:
            return view.viewport().width()
        return option.rect.width()

    def _text_left_offset(self, option, index, line_height):
        """
        Distance from the item's left edge to where its text starts, i.e. the
        room taken by the checkbox and the layer icon.

        The style is asked using a probe rectangle rather than the item's own
        rect: sizeHint() can be called before a row has a usable rect, and
        sizeHint() and paint() must agree on the width or the text is laid out
        for one width and drawn at another, losing the last line.
        """
        indent = self._indentation(index)
        # Keyed by depth only: the offset depends on the checkbox and icon, not
        # on how wide the panel is. Asking QGIS's stylesheet style costs about
        # 50 microseconds, which is far too much to repeat per row per resize.
        cache_key = (indent, line_height)
        offset = self._offset_cache.get(cache_key)
        if offset is None:
            style = option.widget.style() if option.widget else QApplication.style()
            probe = QStyleOptionViewItem(option)
            self.initStyleOption(probe, index)
            # A generous probe width keeps the result independent of the panel
            # width, so the cache survives a resize.
            probe.rect = QRect(0, 0, 10000, max(1, line_height))
            text_rect = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemText, probe, option.widget
            )
            offset = max(0, text_rect.left())
            self._offset_cache[cache_key] = offset
        width = max(32, self._viewport_width(option) - indent)
        return min(offset, max(0, width - 32))

    def _wraps(self, index):
        """
        True when this row's text should be wrapped. This covers layers, groups
        and the symbology (legend) rows underneath a layer.
        """
        return self.wrap_enabled

    def _breakable(self, text):
        """
        Returns the text with zero-width spaces after the characters layer names
        are usually built from, so wrapping prefers to break there instead of
        mid-word ("ovezeti_jel_terv_" + "feliratok" rather than
        "ovezeti_jel_terv_felirato" + "k"). The inserted characters have no
        width, so the name still reads exactly as before.
        """
        if not text:
            return text
        parts = []
        for char in text:
            parts.append(char)
            if char in WRAP_BREAK_CHARS:
                parts.append("​")
        return "".join(parts)

    def _measure(self, text, font, width):
        """
        Lays the text out at the given width and returns (rect, flags).

        Word wrapping is tried first so breaks land on the name's own
        separators; if a piece still does not fit (a name with no separators),
        it is measured again allowing a break anywhere.
        """
        cache_key = (text, width)
        cached = self._measure_cache.get(cache_key)
        if cached is not None:
            height, flags = cached
            return QRect(0, 0, width, height), flags

        metrics = QFontMetrics(font)
        rect = metrics.boundingRect(QRect(0, 0, width, 10000), WRAP_TEXT_FLAGS, text)
        flags = WRAP_TEXT_FLAGS
        if rect.width() > width:
            rect = metrics.boundingRect(
                QRect(0, 0, width, 10000), WRAP_TEXT_FLAGS_ANYWHERE, text
            )
            flags = WRAP_TEXT_FLAGS_ANYWHERE
        if len(self._measure_cache) > 8000:
            self._measure_cache.clear()
        self._measure_cache[cache_key] = (rect.height(), flags)
        return rect, flags

    def _wrapped_rect(self, opt, index, line_height):
        """Bounding rect of the wrapped name text."""
        width = self._wrap_width(opt, index, line_height)
        rect, _flags = self._measure(self._breakable(opt.text or ""), opt.font, width)
        return rect

    def helpEvent(self, event, view, option, index):
        """
        Shows the indicator's tooltip ("Temporary scratch layer only!", the
        filter expression, and so on). QGIS serves these from its own item
        delegate, which this plugin replaces, so they are served here instead.
        """
        try:
            if event.type() == QEvent.Type.ToolTip:
                line_height = QStyledItemDelegate.sizeHint(self, option, index).height()
                for indicator, _icon, rect in self._right_items(
                    index, line_height, option.rect.top(), view.viewport().width()
                ):
                    if indicator is not None and rect.contains(event.pos()):
                        tooltip = indicator.toolTip()
                        if tooltip:
                            QToolTip.showText(event.globalPos(), tooltip, view)
                            return True
        except Exception as e:
            log_message(f"Error showing indicator tooltip: {str(e)}")
        return super().helpEvent(event, view, option, index)

    def sizeHint(self, option, index):
        """
        Extends the default size hint to use the full width of the viewport,
        and makes the row taller when its name needs more than one line.

        The width is the viewport width minus this row's indentation: QGIS sizes
        the column to the widest item, so returning the full viewport width here
        would push the column past the panel and add a horizontal scroll bar.
        """
        if self.resizing and self.wrap_enabled:
            # Fast path for a drag in progress: the row keeps the height it was
            # last measured at, so neither this plugin nor Qt's own sizeHint
            # has to do any work per row. The names re-wrap when the drag stops.
            known = self._row_heights.get(self._index_key(index))
            if known is not None:
                return QSize(
                    max(40, self._viewport_width(option) - self._indentation(index)),
                    known,
                )

        base_size = super().sizeHint(option, index)
        view = self.layer_color_plugin.layer_tree_view
        viewport_width = None
        if option.widget:
            viewport_width = option.widget.viewport().width()
        elif view:
            viewport_width = view.viewport().width()

        height = base_size.height()
        if self._wraps(index):
            key = self._index_key(index)
            known = self._row_heights.get(key)
            if self.resizing and known is not None:
                # Mid-drag: reuse the height this row already had. Re-measuring
                # every row at every intermediate width is what made dragging
                # the panel slow with a large project. A row we have no height
                # for is still measured, so rows never collapse to one line
                # just because a resize happened to be in progress.
                height = max(height, known)
            else:
                # The display text is read from the model rather than through
                # initStyleOption(), which is far too costly to run per row on
                # every relayout.
                text = index.data(Qt.ItemDataRole.DisplayRole)
                if text:
                    width = self._wrap_width(option, index, base_size.height())
                    rect, _flags = self._measure(self._breakable(text), option.font, width)
                    height = max(height, rect.height() + 4)
                if len(self._row_heights) > 8000:
                    self._row_heights.clear()
                self._row_heights[key] = height

        if viewport_width is None:
            return QSize(base_size.width(), height)
        return QSize(max(40, viewport_width - self._indentation(index)), height)

    def _draw_right_items(self, painter, opt, option, index, line_height, invert_icon,
                          row_background=None):
        """
        Draws QGIS's layer tree indicators (or, when it reports none, this
        plugin's own status icon) at the right edge of the row.
        """
        available_width = self._viewport_width(option)
        for indicator, icon, icon_rect in self._right_items(
            index, line_height, opt.rect.top(), available_width
        ):
            if icon is None or icon.isNull():
                continue
            if indicator is None:
                # The plugin's own monochrome icon: inverted on a dark
                # background, as before.
                size = icon.actualSize(icon_rect.size())
                drawn = QRect(
                    icon_rect.left() + (icon_rect.width() - size.width()) // 2,
                    icon_rect.top() + (icon_rect.height() - size.height()) // 2,
                    size.width(),
                    size.height(),
                )
                if invert_icon:
                    pixmap = icon.pixmap(size)
                    image = pixmap.toImage()
                    image.invertPixels()
                    painter.drawPixmap(drawn, QPixmap.fromImage(image))
                else:
                    icon.paint(painter, drawn, Qt.AlignmentFlag.AlignCenter)
                continue

            # QGIS indicators are full-color icons, so they are not inverted.
            # They sit on a chip in the row's own background color, the way QGIS
            # backs them, so they stay legible when text runs under them.
            if opt.state & QStyle.StateFlag.State_Selected:
                chip = opt.palette.color(QPalette.ColorRole.Highlight)
            elif invert_icon:
                # Dark highlight color: a light chip keeps the icon readable
                chip = opt.palette.color(QPalette.ColorRole.Base)
            elif row_background is not None:
                chip = row_background
            else:
                chip = opt.palette.color(QPalette.ColorRole.Base)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(chip)
            painter.drawRoundedRect(icon_rect.adjusted(1, 1, -1, -1), 2, 2)
            painter.restore()

            mode = QIcon.Mode.Normal
            if opt.state & QStyle.StateFlag.State_Selected:
                mode = QIcon.Mode.Selected
            icon.paint(
                painter,
                icon_rect.adjusted(1, 1, -1, -1),
                Qt.AlignmentFlag.AlignCenter,
                mode,
            )

    def paint(self, painter, option, index):
        """
        Custom painting of each layer tree item, including setting a background color
        based on the node's highlight color and drawing an icon if the node represents a layer.
        Now also inverts the icon colors when the background is dark.
        """
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        invert_icon = False  # Flag to decide if the icon should be inverted
        chosen_text_color = None  # Set when a highlight color dictates the text color
        row_background = None  # The node's highlight color, when it has one

        # Get the node corresponding to the current index
        node = self.layer_color_plugin.layer_tree_view.index2node(index)
        if node:
            color = node.customProperty('highlight_color', None)
            if color:
                bg = QColor(color)
                row_background = bg
                opt.backgroundBrush = QBrush(bg)

                # Helper function to compute the luminance of a color
                def get_luminance(c):
                    r, g, b = c.red() / 255.0, c.green() / 255.0, c.blue() / 255.0
                    r = r / 12.92 if r <= 0.03928 else ((r + 0.055) / 1.055) ** 2.4
                    g = g / 12.92 if g <= 0.03928 else ((g + 0.055) / 1.055) ** 2.4
                    b = b / 12.92 if b <= 0.03928 else ((b + 0.055) / 1.055) ** 2.4
                    return 0.2126 * r + 0.7152 * g + 0.0722 * b

                # Helper function to compute the contrast ratio between two colors
                def contrast_ratio(c1, c2):
                    lum1 = get_luminance(c1)
                    lum2 = get_luminance(c2)
                    lighter = max(lum1, lum2)
                    darker = min(lum1, lum2)
                    return (lighter + 0.05) / (darker + 0.05)

                ratio_black = contrast_ratio(bg, QColor(0, 0, 0))
                ratio_white = contrast_ratio(bg, QColor(255, 255, 255))
                # Choose text color based on WCAG guidelines
                if ratio_black >= 4.5 and ratio_black >= ratio_white:
                    chosen_text_color = QColor(0, 0, 0)
                elif ratio_white >= 4.5:
                    chosen_text_color = QColor(255, 255, 255)
                else:
                    chosen_text_color = QColor(0, 0, 0) if ratio_black >= ratio_white else QColor(255, 255, 255)
                opt.palette.setColor(QPalette.ColorRole.Text, chosen_text_color)

                # If white text is chosen, assume the background is dark and set the flag to invert the icon.
                if chosen_text_color == QColor(255, 255, 255):
                    invert_icon = True

        style = option.widget.style() if option.widget else QApplication.style()

        # Height of a single line of text for this row, used to size the status
        # icon and to position wrapped text. Taken before the row was made
        # taller so a wrapped row does not get an oversized icon.
        line_height = super().sizeHint(option, index).height()

        if self._wraps(index) and opt.text:
            # Qt's own item drawing only elides to one line, and its WrapText
            # feature breaks at whitespace only, so the name text is drawn here
            # instead: let the style draw the background/selection with the text
            # suppressed, then draw the wrapped text on top.
            # Same offset sizeHint() used, so the text is drawn at the width it
            # was measured for
            text_left = opt.rect.left() + self._text_left_offset(option, index, line_height)
            raw_text = opt.text
            text = self._breakable(raw_text)
            opt.text = ""
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, option.widget)

            wrap_width = self._wrap_width(option, index, line_height)
            needed, text_flags = self._measure(text, opt.font, wrap_width)

            if needed.height() > opt.rect.height() - 2:
                # The row is shorter than the wrapped text needs, which happens
                # if the view has not applied new row heights yet. Draw a single
                # elided line instead of silently cutting the last line off.
                metrics = QFontMetrics(opt.font)
                elided = metrics.elidedText(
                    raw_text, Qt.TextElideMode.ElideRight, max(16, wrap_width)
                )
                painter.save()
                if opt.state & QStyle.StateFlag.State_Selected:
                    painter.setPen(opt.palette.color(QPalette.ColorRole.HighlightedText))
                elif chosen_text_color is not None:
                    painter.setPen(chosen_text_color)
                else:
                    painter.setPen(opt.palette.color(QPalette.ColorRole.Text))
                painter.drawText(
                    QRect(text_left, opt.rect.top(), wrap_width, opt.rect.height()),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    elided,
                )
                painter.restore()
                self._draw_right_items(painter, opt, option, index, line_height,
                                       invert_icon, row_background)
                return
            # Center a single line vertically; start multi-line text at the top
            align = (
                Qt.AlignmentFlag.AlignVCenter
                if needed.height() <= line_height
                else Qt.AlignmentFlag.AlignTop
            )

            if opt.state & QStyle.StateFlag.State_Selected:
                pen_color = opt.palette.color(QPalette.ColorRole.HighlightedText)
            elif chosen_text_color is not None:
                pen_color = chosen_text_color
            else:
                pen_color = opt.palette.color(QPalette.ColorRole.Text)

            painter.save()
            painter.setPen(pen_color)
            painter.drawText(
                QRect(
                    text_left,
                    opt.rect.top() + 2,
                    wrap_width,
                    opt.rect.height() - 4,
                ),
                text_flags | align,
                text,
            )
            painter.restore()
        else:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, option.widget)

        self._draw_right_items(painter, opt, option, index, line_height, invert_icon,
                               row_background)
