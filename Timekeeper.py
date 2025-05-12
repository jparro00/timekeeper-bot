import discord
from discord.ext import tasks
from discord import app_commands
from datetime import datetime, time as dtime
import re
import os
import json
from pystray import Icon, MenuItem, Menu
from PIL import Image
import threading
import ctypes
import sys
from collections import deque
import base64
from io import BytesIO
from openai import OpenAI
import sys
import asyncio
from constants import EMBEDDED_ICON, TIMEKEEPER_INSTRUCTIONS, STATUS_INSTRUCTIONS, CHAT_INSTRUCTIONS
from tokens import API_KEY, TOKEN


# Initialize the OpenAI client

client = OpenAI(
    api_key=API_KEY)


# Function to interact with Timekeeper
def timekeeper_directive(input_text, input_instructions=None):
    if input_instructions is None:
        input_instructions = TIMEKEEPER_INSTRUCTIONS

    # Create the chat completion
    completion = client.chat.completions.create(
        model="gpt-4o",  # Replace with the appropriate model name
        messages=[
            {"role": "system", "content": input_instructions},
            {"role": "user", "content": input_text}
        ]
    )

    # Return the generated content
    return completion.choices[0].message.content

# Constant for status update interval (in seconds)
MINUTES = 60
HOURS = 3600
DAYS = 86,400
STATUS_UPDATE_INTERVAL = 24 * HOURS

# Control flag for managing the loop
status_update_event = asyncio.Event()

async def update_status_loop():
    status_update_event.set()  # Enable the loop initially
    while status_update_event.is_set():  # Continue only while the event is set
        try:
            # Get the status message from timekeeper_directive
            status_message = timekeeper_directive("", STATUS_INSTRUCTIONS)

            # Determine the activity type based on the message's start
            if status_message.startswith("Playing"):
                activity_type = discord.ActivityType.playing
                status_message = status_message[len("Playing "):].strip()  # Remove the prefix
            elif status_message.startswith("Listening to"):
                activity_type = discord.ActivityType.listening
                status_message = status_message[len("Listening to "):].strip()
            elif status_message.startswith("Streaming"):
                activity_type = discord.ActivityType.streaming
                status_message = status_message[len("Streaming "):].strip()
            elif status_message.startswith("Watching"):
                activity_type = discord.ActivityType.watching
                status_message = status_message[len("Watching "):].strip()
            elif status_message.startswith("Competing in"):
                activity_type = discord.ActivityType.competing
                status_message = status_message[len("Competing in "):].strip()
            else:
                # Default behavior: prepend ". " and set activity type to Watching
                activity_type = discord.ActivityType.watching
                status_message = " the hourglass drain its last grain."

            # Update the bot's presence
            activity = discord.Activity(type=activity_type, name=status_message)
            await bot.change_presence(status=discord.Status.online, activity=activity)

            print(f"Updated status: {status_message} (Type: {activity_type.name})")
        except Exception as e:
            print(f"[ERROR] Failed to update status: {e}")

        # Wait for the next update
        await asyncio.sleep(STATUS_UPDATE_INTERVAL)


intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)  # For slash commands

CONFIG_FILE = "timekeeper_bot_config.json"
reminder_tasks = {}


# Circular buffer for console output
class CircularBuffer:
    def __init__(self, max_lines=1000):
        self.buffer = deque(maxlen=max_lines)

    def write(self, message):
        self.buffer.append(message)

    def flush(self):
        pass

    def replay(self):
        return ''.join(self.buffer)


# Initialize the circular buffer
console_buffer = CircularBuffer()

# Redirect output to the buffer
sys.stdout = console_buffer
sys.stderr = console_buffer


# Load configuration from file
def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("[DEBUG] Config file not found or corrupted. Creating a new one.")
        return {"guilds": {}}


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)


def load_guild_config(guild_id):
    config = load_config()
    guild_config = config.get("guilds", {}).get(str(guild_id), {
        "channel_id": None,
        "reminder_time": None,
        "reminder_message": None,
        "chatbot_prompt": None,
        "message_mode": "static",  # Default to 'static'
        "last_sent_date": None
    })
    return guild_config


def save_guild_config(guild_id, guild_config):
    config = load_config()
    if "guilds" not in config:
        config["guilds"] = {}
    config["guilds"][str(guild_id)] = guild_config
    save_config(config)


async def reminder_task(guild_id):
    guild_config = load_guild_config(guild_id)
    now = datetime.now()
    if guild_config["last_sent_date"] == now.date().isoformat():
        return
    reminder_time = guild_config["reminder_time"]
    if reminder_time:
        try:
            if reminder_time["type"] == "daily":
                task_time = dtime.fromisoformat(reminder_time["time"])
                if now.time().hour == task_time.hour and now.time().minute == task_time.minute:
                    await send_reminder(guild_id, now)
            elif reminder_time["type"] == "weekly":
                task_time = dtime.fromisoformat(reminder_time["time"])
                if now.strftime("%A") == reminder_time[
                    "day"] and now.time().hour == task_time.hour and now.time().minute == task_time.minute:
                    await send_reminder(guild_id, now)
            elif reminder_time["type"] == "specific":
                task_datetime = datetime.fromisoformat(reminder_time["datetime"])
                if now.date() == task_datetime.date() and now.time().hour == task_datetime.time().hour and now.time().minute == task_datetime.time().minute:
                    await send_reminder(guild_id, now)
        except (KeyError, ValueError):
            print(f"[ERROR] Invalid reminder time configuration for guild {guild_id}")


async def send_reminder(guild_id, now):
    guild_config = load_guild_config(guild_id)
    channel_id = guild_config["channel_id"]
    message_mode = guild_config.get("message_mode", "static")  # Default to 'static' if not set

    # Determine the message based on the mode
    if message_mode == "prompt":
        prompt = guild_config.get("chatbot_prompt")
        if not prompt:
            print(f"[ERROR] No prompt set for guild {guild_id}. Skipping reminder.")
            return
        reminder_message = timekeeper_directive(prompt)  # Call your chatbot function
    elif message_mode == "static":
        reminder_message = guild_config.get("reminder_message")
        if not reminder_message:
            print(f"[ERROR] No static message set for guild {guild_id}. Skipping reminder.")
            return
    else:
        print(f"[ERROR] Invalid message mode for guild {guild_id}. Skipping reminder.")
        return

    # Send the message to the designated channel
    channel = bot.get_channel(channel_id)
    if channel:
        try:
            await channel.send(reminder_message)
            guild_config["last_sent_date"] = now.date().isoformat()
            save_guild_config(guild_id, guild_config)
        except discord.Forbidden:
            print(f"[ERROR] Cannot send messages in the channel for guild {guild_id}.")
        except discord.HTTPException as e:
            print(f"[ERROR] HTTP error for guild {guild_id}: {e}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    asyncio.create_task(update_status_loop())  # Start the status updater loop
    print("Status updater started.")
    print("Bot is ready to accept commands!")
    try:
        await tree.sync()
        print("Slash commands synced successfully.")
    except Exception as e:
        print(f"[ERROR] Error syncing commands: {e}")

    # Automatically start reminders for all configured guilds
    config = load_config()
    for guild_id, guild_config in config.get("guilds", {}).items():
        guild_id = int(guild_id)  # Convert guild ID back to an integer
        if guild_config.get("channel_id") and guild_config.get("reminder_time") and guild_config.get(
                "reminder_message"):
            print(f"[DEBUG] Starting reminder for guild {guild_id}...")
            start_guild_reminder(guild_id)  # Reuse the helper function
        else:
            print(f"[DEBUG] Skipping guild {guild_id}: Incomplete configuration.")

    print("All reminders initialized.")


@tree.command(name="channel_set", description="Set the channel where the reminder will be sent.")
async def channel_set(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = interaction.guild_id
    guild_config = load_guild_config(guild_id)

    guild_config["channel_id"] = channel.id
    save_guild_config(guild_id, guild_config)

    await interaction.response.send_message(f"Reminder channel set to #{channel.name}", ephemeral=True)


@tree.command(name="message_set", description="Set the reminder message content.")
async def message_set(interaction: discord.Interaction, message: str):
    guild_id = interaction.guild_id
    guild_config = load_guild_config(guild_id)

    # Process the raw input to avoid escaping
    processed_message = message.replace("\\n", "\n")  # Interpret `\n` as actual line breaks

    guild_config["reminder_message"] = processed_message
    save_guild_config(guild_id, guild_config)

    await interaction.response.send_message(f"Reminder message set to:\n{processed_message}", ephemeral=True)


@tree.command(name="time_set", description="Set the time for the reminder (daily, weekly, or specific date).")
async def time_set(interaction: discord.Interaction, time: str):
    guild_id = interaction.guild_id
    guild_config = load_guild_config(guild_id)

    try:
        # Daily format: HH:MM
        if re.match(r"^\d{2}:\d{2}$", time):
            guild_config["reminder_time"] = {"type": "daily", "time": time}
        # Weekly format: Day HH:MM
        elif re.match(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) \d{2}:\d{2}$", time, re.IGNORECASE):
            day, time_str = time.split(" ")
            guild_config["reminder_time"] = {"type": "weekly", "day": day.capitalize(), "time": time_str}
        # Specific date and time: YYYY-MM-DD HH:MM
        elif re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", time):
            guild_config["reminder_time"] = {"type": "specific", "datetime": time}
        else:
            raise ValueError("Invalid time format.")
        save_guild_config(guild_id, guild_config)
        await interaction.response.send_message(f"Reminder time set successfully!", ephemeral=True)
    except ValueError:
        await interaction.response.send_message(
            "Invalid time format! Use:\n"
            "- `HH:MM` for daily reminders.\n"
            "- `Day HH:MM` for weekly reminders.\n"
            "- `YYYY-MM-DD HH:MM` for specific reminders.",
            ephemeral=True
        )


def start_guild_reminder(guild_id):
    """Start a reminder task for a specific guild if it's not already running."""
    if guild_id in reminder_tasks and reminder_tasks[guild_id].is_running():
        print(f"[DEBUG] Reminder task for guild {guild_id} is already running.")
        return

    # Define the async task for sending reminders
    async def run_reminder_task():
        await reminder_task(guild_id)

    # Create and start the reminder task
    reminder_tasks[guild_id] = tasks.loop(seconds=10)(run_reminder_task)
    reminder_tasks[guild_id].start()
    print(f"[DEBUG] Reminder task for guild {guild_id} started.")


@tree.command(name="start_reminder", description="Start the reminder task.")
async def start_reminder(interaction: discord.Interaction):
    guild_id = interaction.guild_id

    # Start the reminder task for the guild
    start_guild_reminder(guild_id)

    await interaction.response.send_message("Reminder task started!", ephemeral=True)


@tree.command(name="stop_reminder", description="Stop the reminder task.")
async def stop_reminder(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    guild_config = load_guild_config(guild_id)
    # Reset last_sent_date in the guild's configuration
    guild_config["last_sent_date"] = None
    save_guild_config(guild_id, guild_config)

    # Stop the reminder task if it is running
    if guild_id in reminder_tasks and reminder_tasks[guild_id].is_running():
        reminder_tasks[guild_id].stop()

        await interaction.response.send_message("Reminder task stopped, and last sent date has been reset!",
                                                ephemeral=True)
    else:
        await interaction.response.send_message("No reminder task is currently running.", ephemeral=True)


@tree.command(name="show_config", description="Show the current configuration of the reminder bot.")
async def show_config(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    guild_config = load_guild_config(guild_id)

    channel_id = guild_config["channel_id"]
    reminder_time = guild_config["reminder_time"]
    reminder_message = guild_config["reminder_message"]
    chatbot_prompt = guild_config["chatbot_prompt"]
    message_mode = guild_config.get("message_mode", "static")  # Default to 'static'
    last_sent_date = guild_config["last_sent_date"]

    channel_name = f"<#{channel_id}>" if channel_id else "Not set"
    time_info = "Not set"
    if reminder_time:
        if reminder_time["type"] == "daily":
            time_info = f"Daily at {reminder_time['time']}"
        elif reminder_time["type"] == "weekly":
            time_info = f"Every {reminder_time['day']} at {reminder_time['time']}"
        elif reminder_time["type"] == "specific":
            time_info = f"On {reminder_time['datetime']}"

    last_sent = last_sent_date if last_sent_date else "Never"
    is_running = "Yes" if guild_id in reminder_tasks and reminder_tasks[guild_id].is_running() else "No"

    config_message = (
        f"**Current Configuration:**\n"
        f"📢 **Channel:** {channel_name}\n"
        f"⏰ **Reminder Time:** {time_info}\n"
        f"💬 **Message:** {reminder_message if reminder_message else 'Not set'}\n"
        f"🤖 **Chatbot Prompt:** {chatbot_prompt if chatbot_prompt else 'Not set'}\n"
        f"⚙️ **Message Mode:** {message_mode.capitalize()}\n"
        f"📅 **Last Sent Date:** {last_sent}\n"
        f"🏃 **Script Currently Running:** {is_running}"
    )

    await interaction.response.send_message(config_message, ephemeral=True)


@tree.command(name="help", description="Get instructions on how to use the Timekeeper Bot.")
async def help(interaction: discord.Interaction):
    instructions = (
        "The Timekeeper Bot sends scheduled reminders to a designated channel in your server*. "
        "You can set the message, time, and frequency (daily, weekly, or specific dates).\n\n"
        "- **Set the channel** (required) using `/channel_set`.\n"
        "- **Set the reminder message** (required) using `/message_set`.\n"
        "- **Schedule the reminder time** (required) using `/time_set`:\n"
        "  - **Daily**: Use the format `HH:MM` (24-hour time). Example: `/time_set 14:00`.\n"
        "  - **Weekly**: Use the format `Day HH:MM`. Example: `/time_set Monday 09:00`.\n"
        "  - **Specific Date**: Use the format `YYYY-MM-DD HH:MM`. Example: `/time_set 2025-01-30 15:30`.\n"
        "- **Start the reminders** (required) with `/start_reminder`.\n"
        "- **Stop the reminders** with `/stop_reminder`.\n"
        "- **View the current settings** with `/show_config`.\n"
        "- **View this message** with `/help`.\n"
        "\n"
        "*Only one reminder message is sent per day. To reset the reminders for today, use the `/stop_reminder` command followed by the `/start_reminder` command."
    )
    await interaction.response.send_message(instructions, ephemeral=True)


@tree.command(name="prompt_set", description="Set the chatbot prompt for dynamic reminders.")
async def prompt_set(interaction: discord.Interaction, prompt: str):
    guild_id = interaction.guild_id
    guild_config = load_guild_config(guild_id)

    # Save the prompt to the guild configuration
    guild_config["chatbot_prompt"] = prompt
    save_guild_config(guild_id, guild_config)

    await interaction.response.send_message(f"Chatbot prompt set to:\n{prompt}", ephemeral=True)


@tree.command(name="prompt_test", description="Test the chatbot prompt by generating a sample reminder.")
async def prompt_test(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    guild_config = load_guild_config(guild_id)

    # Retrieve the stored prompt
    prompt = guild_config.get("chatbot_prompt")
    if not prompt:
        await interaction.response.send_message("No prompt is set. Use `/prompt_set` to set a prompt first.", ephemeral=True)
        return

    # Defer the response to prevent timeout
    await interaction.response.defer(ephemeral=True)

    # Call the chatbot API with a hard timeout
    try:
        generated_message = await asyncio.wait_for(
            asyncio.to_thread(timekeeper_directive, prompt),  # Run the blocking API call in a thread
            timeout=15  # 15-second timeout
        )
        await interaction.followup.send(f"Sample reminder generated:\n{generated_message}")
    except asyncio.TimeoutError:
        await interaction.followup.send("The chatbot took too long to respond (timeout: 15 seconds). Please try again.")
    except Exception as e:
        await interaction.followup.send(f"Error generating reminder: {e}")

@tree.command(name="mode_set", description="Set the mode for reminders ('prompt' or 'static').")
async def mode_set(interaction: discord.Interaction, mode: str):
    guild_id = interaction.guild_id
    guild_config = load_guild_config(guild_id)

    # Ensure the mode is valid
    if mode not in ["prompt", "static"]:
        await interaction.response.send_message(
            "Invalid mode. Please specify either 'prompt' or 'static'.", ephemeral=True
        )
        return

    # Validate the required data based on the selected mode
    if mode == "prompt" and not guild_config.get("chatbot_prompt"):
        await interaction.response.send_message(
            "Cannot switch to 'prompt' mode because no prompt is set. Use `/prompt_set` to set a prompt first.",
            ephemeral=True
        )
        return

    if mode == "static" and not guild_config.get("reminder_message"):
        await interaction.response.send_message(
            "Cannot switch to 'static' mode because no message is set. Use `/message_set` to set a message first.",
            ephemeral=True
        )
        return

    # Update the mode in the guild configuration
    guild_config["message_mode"] = mode
    save_guild_config(guild_id, guild_config)

    await interaction.response.send_message(
        f"Reminder mode successfully set to '{mode}'.", ephemeral=True
    )

@tree.command(name="update_status", description="Immediately update the bot's status.")
async def update_status(interaction: discord.Interaction):
    global status_update_event

    # Stop the current loop
    status_update_event.clear()
    print("Stopping the current status update loop...")

    # Wait briefly to ensure the loop has stopped
    await asyncio.sleep(1)

    # Restart the status loop
    asyncio.create_task(update_status_loop())
    await interaction.response.send_message("Status updated and loop restarted.", ephemeral=True)

@bot.event
async def on_message(message: discord.Message):
    # Ignore the bot's own messages
    if message.author == bot.user:
        return

    # Check if the bot was mentioned
    bot_mentioned = bot.user in message.mentions

    # Check if this message is a reply (i.e., it has a reference to another message)
    is_reply = message.reference is not None

    # Check if the bot is mentioned
    if bot_mentioned and not is_reply:
        try:
            # Call timekeeper_directive() and wait for its response
            response = await asyncio.to_thread(timekeeper_directive, message.content, CHAT_INSTRUCTIONS)  # Use a thread for blocking function
            await message.channel.send(response)
        except Exception as e:
            # Handle errors gracefully
            await message.channel.send(content="The mechanisms grind, but the spark is dim. Clarity eludes me in this fleeting moment, I cannot answer.")

    # Process other commands alongside the on_message logic
    await bot.process_commands(message)


# System tray functions

def load_icon():
    """Load the tray icon from embedded Base64 data."""
    try:
        icon_data = base64.b64decode(EMBEDDED_ICON)  # Decode the Base64 string
        return Image.open(BytesIO(icon_data))  # Load the image from the in-memory binary data
    except Exception as e:
        print(f"[ERROR] Failed to load embedded icon: {e}")
        return None


def show_console(icon, item):
    if ctypes.windll.kernel32.AllocConsole():
        sys.stdout = open("CONOUT$", "w")
        sys.stderr = open("CONOUT$", "w")
        print(console_buffer.replay(), end='')


def hide_console(icon, item):
    ctypes.windll.kernel32.FreeConsole()
    sys.stdout = console_buffer
    sys.stderr = console_buffer


def on_exit(icon, item):
    icon.stop()
    ctypes.windll.kernel32.FreeConsole()
    os._exit(0)


def run_tray_icon():
    try:
        print("[DEBUG] Initializing tray icon...")
        icon_image = load_icon()  # Load from embedded Base64
        if icon_image is None:
            print("[ERROR] Tray icon could not be loaded.")
            return

        menu = Menu(
            MenuItem('Show Console', show_console),
            MenuItem('Hide Console', hide_console),
            MenuItem('Exit', on_exit)
        )
        icon = Icon("Discord Bot", icon_image, "Discord Bot", menu)
        print("[DEBUG] Running tray icon...")
        icon.run()
        print("[DEBUG] Tray icon stopped.")
    except Exception as e:
        print(f"[ERROR] Tray icon failed to initialize: {e}")


# Start the tray icon in a separate thread
tray_thread = threading.Thread(target=run_tray_icon, daemon=True)
tray_thread.start()

# Run the bot
bot.run(TOKEN)
