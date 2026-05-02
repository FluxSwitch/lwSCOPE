import dearpygui.dearpygui as dpg
from CustomWindow import CustomWindow as CustomWindowClass

############### User UI START ###############
current_color_index = 0
frame_counter = 0
# Predefined color table
COLOR_TABLE = [
    (255, 87, 87, 255),    # Red
    (87, 255, 87, 255),    # Green
    (87, 87, 255, 255),    # Blue
    (255, 255, 87, 255),   # Yellow
    (255, 87, 255, 255),   # Magenta
    (87, 255, 255, 255),   # Cyan
    (255, 165, 87, 255),   # Orange
    (255, 192, 203, 255),  # Pink
    (128, 0, 128, 255),    # Purple
    (255, 255, 255, 255),  # White
]
def User_create_layout():
    def on_change_text_color(sender, app_data, user_data):
        global current_color_index
        color = COLOR_TABLE[current_color_index]
        current_color_index = (current_color_index + 1) % len(COLOR_TABLE)
        dpg.configure_item("user_demo_text", color=color)
        dpg.set_value("user_status_text", f"Text color changed to {color}")

    dpg.add_text("Hello from CustomWindow example", tag="user_demo_text")
    dpg.add_text("Status: Ready", tag="user_status_text")
    dpg.add_button(label="Change Text Color", callback=on_change_text_color)
    dpg.add_separator()
    dpg.add_text("Frame Counter: 0", tag="user_counter_text")
    dpg.add_separator()
    dpg.add_text("Window Size: N/A", tag="user_window_size_text")


def User_resize_callback(sender, app_data):
    # Get viewport dimensions
    width = dpg.get_viewport_width()
    height = dpg.get_viewport_height()
    dpg.set_value("user_window_size_text", f"Window Size: {width} x {height}")

def User_update_logic():
    global frame_counter
    frame_counter += 1
    dpg.set_value("user_counter_text", f"Frame Counter: {frame_counter}")
############### User UI END #################

def resize_callback(sender, app_data):
    CustomWindow.handle_resize(sender, app_data)
    User_resize_callback(sender, app_data)

def render_loop():
    CustomWindow.handler()
    User_update_logic()
    dpg.render_dearpygui_frame()

# Global CustomWindow instance
CustomWindow = CustomWindowClass()

def main():
    dpg.create_context()
    dpg.create_viewport(title="App", width=800, height=600, decorated=False, resizable=True)


    ''' The following demonstrates how to use CustomWindow class
        to create a custom window with user-defined layout and logic.
    '''
    # Initialize CustomWindow
    CustomWindow.initialize_gui(title_text="CustomWindow")
    # Register user-defined layout creation function
    CustomWindow.register_create_layout(User_create_layout)
    # Create the layout
    CustomWindow.create_layout()
    
    
    dpg.set_viewport_resize_callback(resize_callback)
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.set_primary_window("main_window", True)

    while dpg.is_dearpygui_running():
        render_loop()

    dpg.destroy_context()


if __name__ == "__main__":
    main()
