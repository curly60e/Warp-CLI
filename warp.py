import curses
import subprocess
import json
import threading
import time

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
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return json.loads(result.stdout)  # Parse JSON output
        except subprocess.CalledProcessError as e:
            return f"Error executing {command}: {e.stderr.strip()}"
        except json.JSONDecodeError:
            return result.stdout.strip() or "Error: Empty response from command."
        except FileNotFoundError:
            return "Error: lightning-cli not found. Is the Lightning node running?"
        except Exception as e:
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
        except Exception:
            self.node_active = False
            self.current_block_height = "Error"

    def check_wallet_status(self):
        """Check if the wallet is active by trying to list funds."""
        try:
            result = self.run_command("listfunds")
            self.wallet_active = isinstance(result, dict) and "outputs" in result
            if self.wallet_active:
                self.channels = result.get("channels", [])
        except Exception:
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
            self.result_output = "Error: Unable to display help menu."

    def draw_balance_panel(self):
        """Draw the right panel with on-chain and lightning balance, peers, and channels."""
        try:
            onchain_balance, lightning_balance = self.node.get_balances()

            if isinstance(onchain_balance, str) or isinstance(lightning_balance, str):
                # If there's an error message, display it
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
            self.result_output = "Error: Unable to display balances."

    def draw_block_height(self):
        """Draw the current block height on the interface."""
        try:
            block_height = self.node.get_block_height()
            label = f"Cypherpunk Warp Height: {block_height}"
            self.stdscr.addstr(self.max_y - 3, self.max_x - len(label) - 1, label)
        except curses.error:
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
            self.result_output = f"Error: {str(e)}"

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
        else:
            # Run the command and update the result output
            response = self.node.run_command(command_name, params)

            if isinstance(response, str):
                self.result_output = response
            else:
                self.result_output = json.dumps(response, indent=4)

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
        print(f"An unexpected error occurred: {str(e)}")
