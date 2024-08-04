import curses
import subprocess
import json
import threading
import time
import textwrap
import tempfile
import logging
import pyperclip

# Configuración básica de logging para guardar en un archivo
logging.basicConfig(
    filename='debug.log',  # Nombre del archivo de log
    level=logging.DEBUG,  # Nivel de logging
    format='%(asctime)s - %(levelname)s - %(message)s'  # Formato del mensaje
)

class LightningNode:
    def __init__(self, lightning_dir="~/.lightning", network="bitcoin"):
        self.lightning_dir = lightning_dir
        self.network = network
        self.node_active = False
        self.wallet_active = False
        self.current_block_height = None
        self.num_peers = 0
        self.channels = []

    def run_command(self, command, params=[]):
        """Run a lightning-cli command with optional parameters."""
        try:
            # Construct the command
            cmd = ["lightning-cli", f"--network={self.network}", command] + params
            logging.debug(f"Running command: {' '.join(cmd)}")  # Log the command execution
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            # Check for empty response
            if not result.stdout.strip():
                logging.error("Error: Empty response from command.")
                return "Error: Empty response from command."

            return json.loads(result.stdout)  # Parse JSON output
        except subprocess.CalledProcessError as e:
            logging.error(f"Error executing {command}: {e.stderr.strip()}")
            return f"Error executing {command}: {e.stderr.strip()}"
        except json.JSONDecodeError:
            logging.error("Error: Unable to parse JSON response.")
            return "Error: Unable to parse JSON response."
        except FileNotFoundError:
            logging.error("Error: lightning-cli not found. Is the Lightning node running?")
            return "Error: lightning-cli not found. Is the Lightning node running?"
        except Exception as e:
            logging.error(f"Unexpected error: {str(e)}")
            return f"Unexpected error: {str(e)}"

    def get_balances(self):
        """Fetch on-chain and lightning wallet balances."""
        try:
            funds = self.run_command("listfunds")
            if isinstance(funds, str):
                return funds, funds  # Return error message for both balances if it's a string

            channels = funds.get("channels", [])
            outputs = funds.get("outputs", [])

            # Calculate on-chain balance
            onchain_balance = sum(output["amount_msat"] for output in outputs) // 1000

            # Calculate lightning balance
            lightning_balance = sum(channel["our_amount_msat"] for channel in channels) // 1000

            return onchain_balance, lightning_balance
        except Exception as e:
            logging.error(f"Error fetching balances: {str(e)}")
            return "Error fetching balances", str(e)

    def open_channel(self, peer_id, amount, feerate=None):
        """Open a channel with a specified peer."""
        params = [peer_id, str(amount)]
        if feerate:
            params.append(feerate)
        return self.run_command("fundchannel", params)

    def close_channel(self, channel_id, force=False):
        """Close a channel with the given channel ID."""
        params = [channel_id]
        if force:
            params.append("force")
        return self.run_command("close", params)

    def check_node_status(self):
        """Check if the node is active by trying to get node information."""
        try:
            result = self.run_command("getinfo")
            self.node_active = isinstance(result, dict)
            if self.node_active:
                # Update block height and peers if node is active
                self.current_block_height = result.get("blockheight", "Unknown")
                self.num_peers = result.get("num_peers", 0)
        except Exception as e:
            logging.error(f"Error checking node status: {str(e)}")
            self.node_active = False
            self.current_block_height = "Error"

    def check_wallet_status(self):
        """Check if the wallet is active by trying to list funds."""
        try:
            result = self.run_command("listfunds")
            self.wallet_active = isinstance(result, dict) and "outputs" in result
            if self.wallet_active:
                self.channels = result.get("channels", [])
        except Exception as e:
            logging.error(f"Error checking wallet status: {str(e)}")
            self.wallet_active = False
            self.channels = []

    def get_block_height(self):
        """Fetch the current block height using getinfo command."""
        return self.current_block_height if self.current_block_height else "Unknown"

class LightningCLIUI:
    def __init__(self, stdscr, node):
        self.stdscr = stdscr
        self.node = node
        self.command_history = []
        self.current_command = ''
        self.result_output = ''
        self.cursor_x = 0
        self.max_y, self.max_x = self.stdscr.getmaxyx()
        self.show_menu = False
        self.error_message = None
        self.balances_changed = True  # Flag to indicate if balances have changed
        self.init_curses()

        # Start background threads for node and wallet checks
        self.background_thread_running = True
        self.node_thread = threading.Thread(target=self.monitor_node_status, daemon=True)
        self.wallet_thread = threading.Thread(target=self.monitor_wallet_status, daemon=True)
        self.node_thread.start()
        self.wallet_thread.start()

    def init_curses(self):
        """Initialize the curses settings."""
        curses.curs_set(1)  # Show cursor
        self.stdscr.nodelay(1)  # Make getch non-blocking
        self.stdscr.timeout(100)  # Set input timeout for getch
        self.stdscr.clear()
        self.stdscr.refresh()

    def draw_interface(self):
        """Draw the terminal interface."""
        try:
            # Only refresh the screen if there are changes
            if self.show_menu or self.balances_changed or self.result_output or self.current_command:
                # Clear the screen before drawing
                self.stdscr.clear()

                # Draw the result area
                self.stdscr.addstr(0, 0, "Warp Node CLI", curses.A_BOLD)
                self.stdscr.addstr(1, 0, "=" * (self.max_x - 1))

                # Display result or menu based on the state
                if self.show_menu:
                    self.draw_menu()
                else:
                    self.draw_result_output()

                # Draw the command input area
                self.stdscr.addstr(self.max_y - 2, 0, "=" * (self.max_x - 1))
                self.stdscr.addstr(self.max_y - 1, 0, "> " + self.current_command)

                # Draw the balance panel
                self.draw_balance_panel()

                # Draw block height
                self.draw_block_height()

                # Move the cursor to the command input line
                self.stdscr.move(self.max_y - 1, 2 + self.cursor_x)

                # Refresh the screen
                self.stdscr.refresh()

                # Reset change flags after redraw
                self.balances_changed = False
                self.result_output = ''

        except curses.error:
            logging.error("Error: Screen drawing failed, check terminal size.")
            self.result_output = "Error: Screen drawing failed, check terminal size."

    def draw_result_output(self):
        """Display the command result output."""
        try:
            # Draw result area
            lines = self.result_output.split('\n')
            for i, line in enumerate(lines):
                if i + 2 < self.max_y - 2:  # Prevents writing beyond the screen
                    self.stdscr.addstr(2 + i, 0, line[:self.max_x - 1])
        except curses.error:
            logging.error("Error: Unable to display result output.")
            self.result_output = "Error: Unable to display result output."

    def draw_menu(self):
        """Draw the menu with available commands."""
        command_list = [
            "Available Commands:",
            "- getinfo: Get node information",
            "- listfunds: List available funds",
            "- invoice <amt> <label> <desc>: Create invoice",
            "- listinvoices: List all invoices",
            "- pay <bolt11>: Pay an invoice",
            "- offer <amount> <description>: Create an offer",
            "- listpeers: List all peers",
            "- listchannels: List all channels",
            "- connect <id> [host] [port]: Connect to peer",
            "- disconnect <id>: Disconnect from peer",
            "- openchannel <peer_id> <amount> [feerate]: Open a channel",
            "- closechannel <channel_id> [force]: Close a channel",
            "- help: Show this menu",
            "- quit: Exit the CLI",
            "=== Bitcoin Commands ===",
            "- addpsbtoutput <satoshi> [initialpsbt] [locktime] [destination]: Create or modify PSBT with output",
            "- feerates <style>: Get feerate estimates",
            "- fundpsbt <satoshi> <feerate> <startweight> [minconf] [reserve] [locktime] [min_witness_weight] [excess_as_change] [nonwrapped] [opening_anchor_channel]: Create PSBT",
            "- newaddr [addresstype]: Get a new address",
            "- parsefeerate <feerate>: Get current feerate",
            "- reserveinputs <psbt> [exclusive] [reserve]: Reserve utxos",
            "- sendpsbt <psbt> [reserve]: Finalize and send PSBT",
            "- setpsbtversion <psbt> <version>: Convert PSBT version",
            "- signpsbt <psbt> [signonly]: Sign PSBT inputs",
            "- unreserveinputs <psbt> [reserve]: Unreserve utxos",
            "- utxopsbt <satoshi> <feerate> <startweight> <utxos> [reserve] [reservedok] [locktime] [min_witness_weight] [excess_as_change] [opening_anchor_channel]: Create PSBT using utxos",
            "=== Payment Commands ===",
            "- createinvoice <invstring> <label> <preimage>: Sign and create invoice",
            "- createinvoicerequest <bolt12> <savetodb> [exposeid] [recurrence_label] [single_use]: Create and sign invoice_request",
            "- createoffer <bolt12> [label] [single_use]: Create and sign an offer",
            "- createonion <hops> <assocdata> [session_key] [onion_size]: Create an onion",
            "- decodepay <bolt11> [description]: Decode payment",
            "- delinvoice <label> <status> [desconly]: Delete unpaid invoice",
            "- delpay <payment_hash> <status> [partid] [groupid]: Delete payment",
            "- disableinvoicerequest <invreq_id>: Disable invoice_request",
            "- disableoffer <offer_id>: Disable offer",
            "- listinvoicerequests [invreq_id] [active_only]: List invoice requests",
            "- listoffers [offer_id] [active_only]: List offers",
            "- listsendpays [bolt11] [payment_hash] [status] [index] [start] [limit]: List sent payments",
            "- listtransactions: List transactions",
            "- payersign <messagename> <fieldname> <merkle> <tweak>: Sign message",
            "- preapproveinvoice <bolt11>: Preapprove an invoice",
            "- preapprovekeysend <destination> <payment_hash> <amount_msat>: Preapprove a keysend payment",
            "- sendonion <onion> <first_hop> <payment_hash> [label] [shared_secrets] [partid] [bolt11] [amount_msat] [destination] [localinvreqid] [groupid] [description]: Send onion payment",
            "- sendpay <route> <payment_hash> [label] [amount_msat] [bolt11] [payment_secret] [partid] [localinvreqid] [groupid] [payment_metadata] [description] [dev_legacy_hop]: Send payment",
            "- signinvoice <invstring>: Sign invoice",
            "- withdraw destination satoshi [feerate] [minconf] [utxos]: Send funds to {destination} address",
        ]

        # Draw the command list on the screen
        try:
            for i, line in enumerate(command_list):
                if 2 + i < self.max_y - 2:  # Prevent writing beyond screen
                    self.stdscr.addstr(2 + i, 0, line[:self.max_x - 1])
        except curses.error:
            logging.error("Error: Unable to display help menu.")
            self.result_output = "Error: Unable to display help menu."

    def draw_balance_panel(self):
        """Draw the right panel with on-chain and lightning balance, peers, and channels."""
        try:
            onchain_balance, lightning_balance = self.node.get_balances()

            if isinstance(onchain_balance, str) or isinstance(lightning_balance, str):
                # If there's an error message, display it
                logging.error(f"Error fetching balances: {onchain_balance} {lightning_balance}")
                self.result_output = f"Error: {onchain_balance} {lightning_balance}"
                return

            panel_start = self.max_x - 30  # Start drawing panel 30 characters from the right edge

            # Draw the balance panel
            self.stdscr.addstr(0, panel_start, "Wallet Balances", curses.A_BOLD)
            self.stdscr.addstr(1, panel_start, "=" * 28)
            self.stdscr.addstr(2, panel_start, f"On-chain Balance: {onchain_balance} sat")
            self.stdscr.addstr(3, panel_start, f"Lightning Balance: {lightning_balance} sat")

            # Display node status
            node_status = "Active" if self.node.node_active else "Inactive"
            wallet_status = "Active" if self.node.wallet_active else "Inactive"
            self.stdscr.addstr(5, panel_start, f"Node Status: {node_status}")
            self.stdscr.addstr(6, panel_start, f"Wallet Status: {wallet_status}")

            # Display number of peers
            self.stdscr.addstr(8, panel_start, f"Peers: {self.node.num_peers}")

            # Display channels
            self.stdscr.addstr(10, panel_start, "Channels:")
            for i, channel in enumerate(self.node.channels):
                channel_id = channel.get("short_channel_id", "unknown")
                self.stdscr.addstr(11 + i, panel_start, f"- {channel_id}")

        except curses.error:
            logging.error("Error: Unable to display balances.")
            self.result_output = "Error: Unable to display balances."

    def draw_block_height(self):
        """Draw the current block height on the interface."""
        try:
            block_height = self.node.get_block_height()
            label = f"Cypherpunk Warp Height: {block_height}"
            self.stdscr.addstr(self.max_y - 3, self.max_x - len(label) - 1, label)
        except curses.error:
            logging.error("Error: Unable to display block height.")
            self.result_output = "Error: Unable to display block height."

    def run(self):
        """Main loop to handle input and output."""
        try:
            while True:
                self.draw_interface()
                key = self.stdscr.getch()

                if key == curses.ERR:
                    continue

                # Handle key inputs
                if key == curses.KEY_BACKSPACE or key == 127:
                    if self.current_command:  # Avoid extra backspaces when command is empty
                        self.current_command = self.current_command[:-1]
                        self.cursor_x = max(0, self.cursor_x - 1)
                elif key in (curses.KEY_ENTER, 10, 13):
                    if self.current_command.strip() == "help":
                        if not self.show_menu:
                            self.show_menu = True
                            self.balances_changed = True
                    elif self.current_command.strip() == "pay":
                        self.pay_invoice_popup()
                        self.balances_changed = True  # Mark change for redraw
                    else:
                        self.show_menu = False
                        self.execute_command(self.current_command)
                        self.balances_changed = True  # Mark change for redraw
                    self.current_command = ''
                    self.cursor_x = 0
                elif key == 27:  # ESC key to exit
                    self.background_thread_running = False
                    break
                elif 32 <= key <= 126:  # Printable characters
                    self.current_command += chr(key)
                    self.cursor_x += 1

                # Always redraw the command input line
                self.stdscr.addstr(self.max_y - 1, 0, "> " + self.current_command + " " * (self.max_x - len(self.current_command) - 3))
                self.stdscr.move(self.max_y - 1, 2 + self.cursor_x)
                self.stdscr.refresh()

        except Exception as e:
            logging.error(f"Error: {str(e)}")
            self.result_output = f"Error: {str(e)}"

    def show_bolt11_popup(self, bolt11):
        """Display a popup window with the bolt11 invoice code for easy copying."""
        # Calculate the size of the window
        popup_width = min(self.max_x - 4, 80)  # Max width of 80 or screen width minus 4
        popup_height = 10  # Adjust height to accommodate wrapped lines
        start_x = (self.max_x - popup_width) // 2
        start_y = (self.max_y - popup_height) // 2

        # Create the new window
        popup_win = curses.newwin(popup_height, popup_width, start_y, start_x)
        popup_win.border()

        # Add a title
        title = "Invoice Code"
        popup_win.addstr(0, (popup_width - len(title)) // 2, title, curses.A_BOLD)

        # Add the bolt11 text with proper wrapping
        wrapped_bolt11 = textwrap.wrap(bolt11, width=popup_width - 4)
        for idx, line in enumerate(wrapped_bolt11):
            if idx + 2 < popup_height - 1:  # Ensure we don't write beyond the window height
                popup_win.addstr(idx + 2, 2, line)

        # Refresh the popup to display it
        popup_win.refresh()

        # Wait for user input to close the popup
        popup_win.getch()
        popup_win.clear()
        self.stdscr.touchwin()
        self.stdscr.refresh()

    def show_bolt12_popup(self):
        """Display a popup window for entering and processing bolt12 codes."""
        # Calculate appropriate dimensions for the popup window
        popup_height = 10
        popup_width = min(self.max_x - 4, 150)  # Increased max width to accommodate long inputs
        start_y, start_x = (self.max_y - popup_height) // 2, (self.max_x - popup_width) // 2

        # Create a new window for the popup
        popup_win = curses.newwin(popup_height, popup_width, start_y, start_x)
        popup_win.box()
        popup_win.addstr(1, 2, "Enter Offer (Bolt12)", curses.A_BOLD)
        popup_win.addstr(2, 2, "Paste your offer code here:")

        # Initialize input field
        curses.curs_set(1)  # Show cursor
        input_buffer = ""
        cursor_pos = 0  # To manage horizontal scrolling
        scroll_offset = 0

        while True:
            popup_win.refresh()
            key = popup_win.getch()

            if key in (curses.KEY_ENTER, 10, 13):
                # Run fetchinvoice with the entered bolt12 code
                if input_buffer.strip():
                    self.execute_fetchinvoice(input_buffer.strip())
                    break
            elif key in (curses.KEY_BACKSPACE, 127):
                # Handle backspace
                if cursor_pos > 0:
                    input_buffer = input_buffer[:cursor_pos - 1] + input_buffer[cursor_pos:]
                    cursor_pos -= 1
                    if scroll_offset > 0:
                        scroll_offset -= 1
            elif key == curses.KEY_LEFT:
                # Move cursor left
                if cursor_pos > 0:
                    cursor_pos -= 1
                    if cursor_pos < scroll_offset:
                        scroll_offset -= 1
            elif key == curses.KEY_RIGHT:
                # Move cursor right
                if cursor_pos < len(input_buffer):
                    cursor_pos += 1
                    if cursor_pos >= scroll_offset + (popup_width - 4):
                        scroll_offset += 1
            elif key == 27:  # ESC key to exit
                break
            elif 32 <= key <= 126:  # Handle printable characters
                input_buffer = input_buffer[:cursor_pos] + chr(key) + input_buffer[cursor_pos:]
                cursor_pos += 1
                if cursor_pos >= scroll_offset + (popup_width - 4):
                    scroll_offset += 1

            # Clear input line and re-draw it
            popup_win.addstr(3, 2, " " * (popup_width - 4))
            popup_win.addstr(3, 2, input_buffer[scroll_offset:scroll_offset + (popup_width - 4)])
            popup_win.move(3, 2 + cursor_pos - scroll_offset)

        curses.curs_set(0)  # Hide cursor
        del popup_win  # Remove popup window after done


    def execute_fetchinvoice(self, bolt12_code):
        """Execute the fetchinvoice command with the provided bolt12 code."""
        response = self.node.run_command("fetchinvoice", [bolt12_code])

        # Check if the response is valid and display the result
        if isinstance(response, dict):
            self.result_output = self.format_json(response)
        else:
            self.result_output = response


    def execute_command(self, command):
        """Execute the entered command and store the result."""
        if not command.strip():
            return

        self.command_history.append(command)
        parts = command.split()
        command_name = parts[0]
        params = parts[1:]

        if command_name == "quit":
            self.background_thread_running = False
            raise SystemExit

        # Handle specific commands
        if command_name == "openchannel":
            if len(params) < 2:
                self.result_output = "Error: Usage: openchannel <peer_id> <amount> [feerate]"
            else:
                peer_id = params[0]
                amount = params[1]
                feerate = params[2] if len(params) > 2 else None
                self.result_output = json.dumps(self.node.open_channel(peer_id, amount, feerate), indent=4)
        elif command_name == "closechannel":
            if len(params) < 1:
                self.result_output = "Error: Usage: closechannel <channel_id> [force]"
            else:
                channel_id = params[0]
                force = len(params) > 1 and params[1].lower() == "force"
                self.result_output = json.dumps(self.node.close_channel(channel_id, force), indent=4)
        elif command_name == "invoice":
            if len(params) < 3:
                self.result_output = "Error: Usage: invoice <amt> <label> <desc>"
            else:
                amt = str(int(params[0]) * 1000)  # Convert sats to millisats
                label = params[1]
                desc = " ".join(params[2:])

                # Command to create invoice
                response = self.node.run_command("invoice", [amt, label, desc])
                if isinstance(response, dict) and "bolt11" in response:
                    self.show_bolt11_popup(response["bolt11"])
                self.result_output = self.format_json(response)
        elif command_name == "fetchinvoice":
            self.show_bolt12_popup()
        else:
            # Run the command and update the result output
            response = self.node.run_command(command_name, params)

            # Format JSON output with text wrapping
            if isinstance(response, dict):
                formatted_output = self.format_json(response)
                self.result_output = formatted_output
            else:
                self.result_output = response



    def pay_invoice_popup(self):
        """Display a popup window for entering and paying an invoice."""
        popup_height, popup_width = 10, 80
        popup_y, popup_x = (self.max_y - popup_height) // 2, (self.max_x - popup_width) // 2

        popup = curses.newwin(popup_height, popup_width, popup_y, popup_x)
        popup.border()
        popup.addstr(1, 2, "Enter Invoice", curses.A_BOLD)
        popup.addstr(2, 2, "Paste your Lightning invoice here:")
        popup.refresh()

        # Create a subwindow for input
        input_window = curses.newwin(popup_height - 4, popup_width - 4, popup_y + 4, popup_x + 2)
        input_window.clear()
        curses.echo()  # Enable echoing of input

        # Capture multiline input
        invoice_lines = []
        while True:
            line = input_window.getstr().decode('utf-8').strip()
            if not line:
                break
            invoice_lines.append(line)

        # Combine all lines into a single invoice string
        invoice = ''.join(invoice_lines)
        curses.noecho()  # Disable echoing

        # Clear the popup
        popup.clear()
        popup.refresh()

        if invoice:
            response = self.node.run_command("pay", [invoice])

            if isinstance(response, dict):
                formatted_output = self.format_json(response)
                self.result_output = formatted_output
            else:
                self.result_output = response


    def format_json(self, json_data):
        """Format JSON data with proper indentation and text wrapping."""
        try:
            formatted_lines = json.dumps(json_data, indent=4).splitlines()
            wrapped_lines = []
            for line in formatted_lines:
                if len(line) > self.max_x - 1:
                    wrapped_lines.extend(textwrap.wrap(line, width=self.max_x - 1))
                else:
                    wrapped_lines.append(line)
            return "\n".join(wrapped_lines)
        except Exception as e:
            logging.error(f"Error formatting JSON: {str(e)}")
            return f"Error formatting JSON: {str(e)}"

    def monitor_node_status(self):
        """Background thread to check the node status."""
        previous_status = self.node.node_active
        previous_block_height = self.node.current_block_height
        while self.background_thread_running:
            self.node.check_node_status()
            # Mark balances changed if status or block height has changed
            if self.node.node_active != previous_status or self.node.current_block_height != previous_block_height:
                self.balances_changed = True
            previous_status = self.node.node_active
            previous_block_height = self.node.current_block_height
            time.sleep(10)  # Check every 10 seconds

    def monitor_wallet_status(self):
        """Background thread to check the wallet status."""
        previous_wallet_status = self.node.wallet_active
        previous_channels = self.node.channels
        while self.background_thread_running:
            self.node.check_wallet_status()
            # Mark balances changed if wallet status or channels have changed
            if self.node.wallet_active != previous_wallet_status or self.node.channels != previous_channels:
                self.balances_changed = True
            previous_wallet_status = self.node.wallet_active
            previous_channels = self.node.channels
            time.sleep(10)  # Check every 10 seconds

def main(stdscr):
    # Initialize the LightningNode and the UI
    node = LightningNode()
    ui = LightningCLIUI(stdscr, node)

    # Run the UI
    ui.run()

if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        print("\nExiting the program.")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {str(e)}")
        print(f"An unexpected error occurred: {str(e)}")
