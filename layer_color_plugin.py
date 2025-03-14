import os
from qgis.core import QgsMessageLog, Qgis, QgsProject, QgsLayerTree
from PyQt5.QtWidgets import (
    QAction,
    QMenu,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QStyle,
    QApplication,
    QMessageBox,
    QWidgetAction
)
from PyQt5.QtGui import QColor, QPainter, QIcon, QPalette
from PyQt5.QtCore import Qt, QRect, QSize
# Import the QGIS color wheel widget
from qgis.gui import QgsColorWheel

def log_message(message):
    """
    Logs messages to QGIS log panel and Python console.
    """
    QgsMessageLog.logMessage(message, "LayerColorPlugin", Qgis.Info)
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

            # Set a custom delegate to handle item drawing
            self.delegate = LayerColorDelegate(self, self.layer_tree_view)
            self.layer_tree_view.setItemDelegate(self.delegate)

            # Set up a custom context menu on the layer tree view
            self.layer_tree_view.setContextMenuPolicy(Qt.CustomContextMenu)
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
        Unloads the plugin by disconnecting signals and restoring the default delegate.
        Also clears any custom highlight color properties.
        """
        try:
            # Disconnect the custom context menu signal
            if hasattr(self, "layer_tree_view"):
                self.layer_tree_view.customContextMenuRequested.disconnect(self.show_context_menu)

            # Restore the original delegate
            if hasattr(self, "delegate") and self.delegate is not None:
                original_delegate = QStyledItemDelegate(self.layer_tree_view)
                self.layer_tree_view.setItemDelegate(original_delegate)
                self.delegate = None

            # Clear custom highlight_color properties from all nodes
            root = self.iface.layerTreeView().layerTreeModel().rootGroup()

            def clear_custom_color(node):
                if hasattr(node, "customProperty") and node.customProperty("highlight_color", None):
                    node.removeCustomProperty("highlight_color")
                for child in node.children():
                    clear_custom_color(child)

            clear_custom_color(root)

            # Disconnect the project signals
            QgsProject.instance().writeProject.disconnect(self.save_colors)
            self.iface.projectRead.disconnect(self.load_colors)

            # Refresh the layer tree view
            if hasattr(self, "layer_tree_view"):
                self.layer_tree_view.viewport().update()

        except Exception as e:
            log_message(f"Error during unload: {str(e)}")

    def show_context_menu(self, point):
        """
        Displays the context menu when a user right-clicks in the layer tree view.
        Embeds a color wheel (qgis.gui.QgsColorWheel) with proper lifetime management.
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

        # Insert the custom highlight menu into the context menu
        actions = context_menu.actions()
        if actions:
            context_menu.insertMenu(actions[-1], highlight_menu)
        else:
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
        context_menu.exec_(self.layer_tree_view.mapToGlobal(point))

    def set_layer_color_from_widget(self, color):
        """
        Called when the user changes the color in the embedded color wheel.
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
            self.layer_tree_view.viewport().update()
            log_message("All colors restored successfully.")
        except Exception as e:
            log_message(f"Error loading colors: {str(e)}")


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

    def sizeHint(self, option, index):
        """
        Extends the default size hint to use the full width of the viewport.
        """
        base_size = super().sizeHint(option, index)
        if option.widget:
            width = option.widget.viewport().width()
            return QSize(width, base_size.height())
        return base_size

    def paint(self, painter, option, index):
        """
        Custom painting of each layer tree item, including setting a background color
        based on the node's highlight color and drawing an icon if the node represents a layer.
        """
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        # Get the node corresponding to the current index
        node = self.layer_color_plugin.layer_tree_view.index2node(index)
        if node:
            color = node.customProperty('highlight_color', None)
            if color:
                bg = QColor(color)
                opt.backgroundBrush = bg

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

                # Calculate contrast ratios for black and white text against the background
                ratio_black = contrast_ratio(bg, QColor(0, 0, 0))
                ratio_white = contrast_ratio(bg, QColor(255, 255, 255))
                # Choose text color based on WCAG guidelines
                if ratio_black < 4.5:
                    if ratio_white >= 4.5:
                        chosen_text_color = QColor(255, 255, 255)
                    else:
                        chosen_text_color = QColor(0, 0, 0) if ratio_black >= ratio_white else QColor(255, 255, 255)
                    opt.palette.setColor(QPalette.Text, chosen_text_color)

        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, option.widget)

        # If the node represents a layer, draw its corresponding icon on the right
        if node and QgsLayerTree.isLayer(node):
            layer = node.layer()
            icon_to_draw = None
            if not layer.isValid():
                icon_to_draw = self.default_missing_icon
            elif not layer.crs().isValid():
                icon_to_draw = self.default_nocrs_icon
            elif layer.dataProvider().name() == "memory":
                icon_to_draw = self.default_memory_icon
            elif layer.dataProvider().name() == "virtual":
                icon_to_draw = self.default_virtual_icon

            if icon_to_draw and not icon_to_draw.isNull():
                available_width = (option.widget.viewport().width() if option.widget else opt.rect.width())
                icon_size = icon_to_draw.actualSize(opt.rect.size())
                icon_x = available_width - icon_size.width() - 2
                icon_rect = QRect(
                    icon_x,
                    opt.rect.top() + (opt.rect.height() - icon_size.height()) // 2,
                    icon_size.width(),
                    icon_size.height()
                )
                icon_to_draw.paint(painter, icon_rect, Qt.AlignCenter)
