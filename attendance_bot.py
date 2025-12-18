# bot.py
import os
import json
import asyncio
from datetime import datetime, time as dtime, timedelta
import aiosqlite
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from openpyxl import Workbook
from io import BytesIO
from zoneinfo import ZoneInfo
from keep_alive import keep_alive

keep_alive()  # This starts the web server


# ---- CONFIG ----
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None
TZ_NAME = os.getenv("TZ_NAME", "")
TIMEZONE_OFFSET_HOURS = float(os.getenv("TZ_OFFSET_HOURS", "6"))

ADMIN_ROLE = os.getenv("ADMIN_ROLE", "Admin")
ADMIN_MEMBERS = os.getenv("ADMIN_MEMBERS", "").split(",") if os.getenv("ADMIN_MEMBERS") else []
LATE_AFTER = os.getenv("LATE_AFTER", "10:01")
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "18:00")
DB_FILE = os.getenv("DB_FILE", "attendance.sqlite")

LATE_AFTER_T = datetime.strptime(LATE_AFTER, "%H:%M").time()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---- DB SETUP ----
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            date TEXT NOT NULL,
            checkins TEXT DEFAULT '[]',
            checkouts TEXT DEFAULT '[]',
            late INTEGER DEFAULT 0,
            notes TEXT DEFAULT ''
        );
        """)
        await db.commit()

@bot.event
async def on_ready():
    await init_db()
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    daily_report_loop.start()
    print(f"Bot ready as {bot.user} (id: {bot.user.id})")

# ---- HELPERS ----
def now_local():
    if TZ_NAME:
        return datetime.now(ZoneInfo(TZ_NAME))
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET_HOURS)

def today_str():
    return now_local().strftime("%Y-%m-%d")

def time_str():
    return now_local().strftime("%H:%M:%S")

def is_late(checkin_time_str):
    t = datetime.strptime(checkin_time_str, "%H:%M:%S").time()
    return t > LATE_AFTER_T

async def ensure_row(db, user_id, date):
    async with db.execute("SELECT id FROM attendance WHERE user_id=? AND date=?", (user_id, date)) as cursor:
        row = await cursor.fetchone()
    if not row:
        await db.execute("INSERT INTO attendance (user_id, date) VALUES (?, ?)", (user_id, date))
        await db.commit()

def admin_check(member: discord.Member):
    """Check if member has admin access"""
    # Check if user has admin role
    if any(r.name == ADMIN_ROLE for r in member.roles):
        return True
    # Check if user is server admin
    if member.guild_permissions.administrator:
        return True
    # Check if user ID is in ADMIN_MEMBERS list
    if str(member.id).strip() in [uid.strip() for uid in ADMIN_MEMBERS]:
        return True
    return False

# ---- ATTENDANCE ACTIONS (core) ----
async def add_checkin(user_id: str, date: str, tstr: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_row(db, user_id, date)
        async with db.execute("SELECT checkins FROM attendance WHERE user_id=? AND date=?", (user_id, date)) as cursor:
            row = await cursor.fetchone()
            checkins = json.loads(row[0] or "[]")
        checkins.append(tstr)
        late_flag = 1 if is_late(tstr) else 0
        async with db.execute("SELECT late FROM attendance WHERE user_id=? AND date=?", (user_id, date)) as cursor:
            row2 = await cursor.fetchone()
            existing_late = int(row2[0]) if row2 else 0
        new_late = 1 if (existing_late or late_flag) else 0
        await db.execute("UPDATE attendance SET checkins=?, late=? WHERE user_id=? AND date=?", (json.dumps(checkins), new_late, user_id, date))
        await db.commit()
        return late_flag == 1

async def add_checkout(user_id: str, date: str, tstr: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await ensure_row(db, user_id, date)
        async with db.execute("SELECT checkouts FROM attendance WHERE user_id=? AND date=?", (user_id, date)) as cursor:
            row = await cursor.fetchone()
            checkouts = json.loads(row[0] or "[]")
        checkouts.append(tstr)
        await db.execute("UPDATE attendance SET checkouts=? WHERE user_id=? AND date=?", (json.dumps(checkouts), user_id, date))
        await db.commit()

def compute_hours_for_lists(checkins, checkouts):
    total = 0.0
    fmt = "%H:%M:%S"
    for i, ci in enumerate(checkins):
        try:
            ck = checkouts[i] if i < len(checkouts) else None
            if not ck:
                continue
            tci = datetime.strptime(ci, fmt)
            tco = datetime.strptime(ck, fmt)
            delta = (tco - tci).total_seconds()
            if delta > 0:
                total += delta / 3600.0
        except Exception:
            continue
    return total

# ---- COMMANDS (legacy text commands - user only) ----

@bot.command(name="checkin")
async def cmd_checkin(ctx):
    user_id = str(ctx.author.id)
    date = today_str()
    tstr = time_str()
    late = await add_checkin(user_id, date, tstr)
    msg = f"✅ Checked in at {tstr}"
    if late:
        msg += " — ⏰ You are marked **Late** (after " + LATE_AFTER + ")"
    await ctx.send(msg)

@bot.command(name="checkout")
async def cmd_checkout(ctx):
    user_id = str(ctx.author.id)
    date = today_str()
    tstr = time_str()
    await add_checkout(user_id, date, tstr)
    await ctx.send(f"🏁 Checked out at {tstr}")

@bot.command(name="attendance")
async def cmd_attendance(ctx, member: discord.Member = None):
    target = member or ctx.author
    user_id = str(target.id)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT date, checkins, checkouts, late FROM attendance WHERE user_id=? ORDER BY date DESC LIMIT 365", (user_id,)) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await ctx.send(f"No attendance records for **{target.display_name}**")
        return
    days_present = sum(1 for r in rows if json.loads(r[1] or "[]"))
    days_absent = max(0, 30 - days_present)
    total_hours = 0.0
    late_days = 0
    for r in rows:
        checkins = json.loads(r[1] or "[]")
        checkouts = json.loads(r[2] or "[]")
        total_hours += compute_hours_for_lists(checkins, checkouts)
        if int(r[3]):
            late_days += 1
    text = (f"📊 **Attendance Profile — {target.display_name}**\n"
            f"**Days Present (last {len(rows)} records):** {days_present}\n"
            f"**Days Absent (approx):** {days_absent}\n"
            f"**Total Hours Worked:** {total_hours:.2f} hrs\n"
            f"**Late Days:** {late_days}")
    await ctx.send(text)

@bot.command(name="absentlist")
async def cmd_absentlist(ctx):
    date = today_str()
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else ctx.guild
    if not guild:
        await ctx.send("Guild not configured.")
        return
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT user_id FROM attendance WHERE date=?", (date,)) as cursor:
            rows = await cursor.fetchall()
            present_ids = {r[0] for r in rows}
    absent_members = [m for m in guild.members if not m.bot and str(m.id) not in present_ids]
    if not absent_members:
        await ctx.send("Everyone is present today!")
        return
    msg = "\n".join(f"• {m.display_name}" for m in absent_members)
    await ctx.send(f"🚫 **Absent Users Today:**\n{msg}")

@bot.command(name="monthly")
async def cmd_monthly(ctx, member: discord.Member = None, month: str = None):
    target = member or ctx.author
    user_id = str(target.id)
    if not month:
        month = now_local().strftime("%Y-%m")
    like = month + "%"
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT date, checkins, checkouts, late FROM attendance WHERE user_id=? AND date LIKE ? ORDER BY date", (user_id, like)) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await ctx.send(f"No records for {target.display_name} in {month}")
        return
    days_present = sum(1 for r in rows if json.loads(r[1] or "[]"))
    total_hours = 0.0
    late_days = 0
    for r in rows:
        checkins = json.loads(r[1] or "[]")
        checkouts = json.loads(r[2] or "[]")
        total_hours += compute_hours_for_lists(checkins, checkouts)
        if int(r[3]):
            late_days += 1
    unique_days = {r[0] for r in rows}
    days_in_month_seen = len(unique_days)
    await ctx.send((f"📅 **Monthly Summary — {target.display_name} — {month}**\n"
                    f"**Days Present:** {days_present}\n"
                    f"**Days Logged (records):** {days_in_month_seen}\n"
                    f"**Total Hours:** {total_hours:.2f}\n"
                    f"**Late Days:** {late_days}"))

# ---- WELCOME MESSAGE ----

@bot.event
async def on_member_join(member):
    try:
        await member.send(
            f"Welcome to {member.guild.name}! 👋\n"
            "To mark attendance, use `/checkin` and `/checkout` commands.\n"
            f"Note: Check-in after {LATE_AFTER} will be marked as Late."
        )
    except Exception:
        pass

# ---- SLASH COMMANDS (User) ----

@tree.command(name="checkin", description="Check in for today", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
async def app_checkin(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    date = today_str()
    tstr = time_str()
    late = await add_checkin(user_id, date, tstr)
    msg = f"✅ Checked in at {tstr}"
    if late:
        msg += f" — ⏰ Marked **Late** (after {LATE_AFTER})"
    await interaction.response.send_message(msg, ephemeral=False)

@tree.command(name="checkout", description="Check out for today", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
async def app_checkout(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    date = today_str()
    tstr = time_str()
    await add_checkout(user_id, date, tstr)
    await interaction.response.send_message(f"🏁 Checked out at {tstr}", ephemeral=False)

@tree.command(name="profile", description="Show attendance profile", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.describe(member="Member to view (optional)")
async def app_profile(interaction: discord.Interaction, member: discord.Member = None):
    if member and member.id != interaction.user.id:
        if not admin_check(interaction.user):
            await interaction.response.send_message("❌ You can only view your own profile. Admins can view others.", ephemeral=True)
            return
    
    target = member or interaction.user
    user_id = str(target.id)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT date, checkins, checkouts, late FROM attendance WHERE user_id=? ORDER BY date DESC LIMIT 365", (user_id,)) as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await interaction.response.send_message(f"No attendance records for **{target.display_name}**", ephemeral=True)
        return
    days_present = sum(1 for r in rows if json.loads(r[1] or "[]"))
    total_hours = 0.0
    late_days = 0
    for r in rows:
        checkins = json.loads(r[1] or "[]")
        checkouts = json.loads(r[2] or "[]")
        total_hours += compute_hours_for_lists(checkins, checkouts)
        if int(r[3]):
            late_days += 1
    text = (f"📊 **Attendance Profile — {target.display_name}**\n"
            f"**Days Present (last {len(rows)} records):** {days_present}\n"
            f"**Total Hours Worked:** {total_hours:.2f} hrs\n"
            f"**Late Days:** {late_days}")
    await interaction.response.send_message(text, ephemeral=False)

# ---- SLASH COMMANDS (ADMIN ONLY) ----

async def admin_slash_check(interaction: discord.Interaction) -> bool:
    """Check if user is admin"""
    if not admin_check(interaction.user):
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return False
    return True

@tree.command(name="editcheckin", description="Add/Edit check-in for a member (Admin Only)", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.describe(member="Member to edit", date="Date (YYYY-MM-DD)", time="Time (HH:MM:SS)")
async def app_editcheckin(interaction: discord.Interaction, member: discord.Member, date: str, time: str):
    if not await admin_slash_check(interaction):
        return
    
    try:
        datetime.strptime(time, "%H:%M:%S")
        datetime.strptime(date, "%Y-%m-%d")
        user_id = str(member.id)
        await add_checkin(user_id, date, time)
        await interaction.response.send_message(
            f"✅ Added check-in for {member.display_name} on {date} at {time}",
            ephemeral=True
        )
    except ValueError:
        await interaction.response.send_message(
            "❌ Invalid format. Use date: YYYY-MM-DD, time: HH:MM:SS",
            ephemeral=True
        )

@tree.command(name="editcheckout", description="Add/Edit check-out for a member (Admin Only)", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.describe(member="Member to edit", date="Date (YYYY-MM-DD)", time="Time (HH:MM:SS)")
async def app_editcheckout(interaction: discord.Interaction, member: discord.Member, date: str, time: str):
    if not await admin_slash_check(interaction):
        return
    
    try:
        datetime.strptime(time, "%H:%M:%S")
        datetime.strptime(date, "%Y-%m-%d")
        user_id = str(member.id)
        await add_checkout(user_id, date, time)
        await interaction.response.send_message(
            f"✅ Added check-out for {member.display_name} on {date} at {time}",
            ephemeral=True
        )
    except ValueError:
        await interaction.response.send_message(
            "❌ Invalid format. Use date: YYYY-MM-DD, time: HH:MM:SS",
            ephemeral=True
        )

@tree.command(name="removeattendance", description="Remove attendance record (Admin Only)", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.describe(member="Member to remove record for", date="Date (YYYY-MM-DD)")
async def app_removeattendance(interaction: discord.Interaction, member: discord.Member, date: str):
    if not await admin_slash_check(interaction):
        return
    
    try:
        datetime.strptime(date, "%Y-%m-%d")
        user_id = str(member.id)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("DELETE FROM attendance WHERE user_id=? AND date=?", (user_id, date))
            await db.commit()
        await interaction.response.send_message(
            f"🗑️ Removed attendance record for {member.display_name} on {date}",
            ephemeral=True
        )
    except ValueError:
        await interaction.response.send_message(
            "❌ Invalid date format. Use YYYY-MM-DD",
            ephemeral=True
        )

@tree.command(name="exportattendance", description="Export attendance to Excel (Admin Only)", guild=discord.Object(id=GUILD_ID) if GUILD_ID else None)
@app_commands.describe(mode="Export mode: combined or separate", member="Optional: specific member", month="Optional: YYYY-MM")
async def app_exportattendance(interaction: discord.Interaction, mode: str = "combined", member: discord.Member = None, month: str = None):
    if not await admin_slash_check(interaction):
        return
    
    # Validate mode
    if mode not in ["combined", "separate"]:
        await interaction.response.send_message(
            "❌ Mode must be 'combined' or 'separate'",
            ephemeral=True
        )
        return
    
    target_id = str(member.id) if member else None
    guild = interaction.guild

    async with aiosqlite.connect(DB_FILE) as db:
        if target_id and month:
            like = month + "%"
            async with db.execute("SELECT user_id, date, checkins, checkouts, late FROM attendance WHERE user_id=? AND date LIKE ? ORDER BY date", (target_id, like)) as cursor:
                rows = await cursor.fetchall()
        elif target_id:
            async with db.execute("SELECT user_id, date, checkins, checkouts, late FROM attendance WHERE user_id=? ORDER BY date", (target_id,)) as cursor:
                rows = await cursor.fetchall()
        elif month:
            like = month + "%"
            async with db.execute("SELECT user_id, date, checkins, checkouts, late FROM attendance WHERE date LIKE ? ORDER BY user_id, date", (like,)) as cursor:
                rows = await cursor.fetchall()
        else:
            async with db.execute("SELECT user_id, date, checkins, checkouts, late FROM attendance ORDER BY user_id, date") as cursor:
                rows = await cursor.fetchall()

    if not rows:
        await interaction.response.send_message("❌ No attendance records found.", ephemeral=True)
        return

    wb = Workbook()

    if target_id:
        ws = wb.active
        ws.title = "Attendance"
        ws.append(["User ID", "User Name", "Date", "Checkins", "Checkouts", "Late", "Total Late Days"])
        total_late_days = sum(1 for r in rows if int(r[4]))
        for r in rows:
            uid, date, cis, cos, late = r
            member_obj = guild.get_member(int(uid)) if guild else None
            name = member_obj.display_name if member_obj else uid
            ws.append([uid, name, date, ", ".join(json.loads(cis or "[]")), ", ".join(json.loads(cos or "[]")), "Yes" if int(late) else "No", total_late_days])
    
    elif mode == "combined":
        ws = wb.active
        ws.title = "All Attendance"
        ws.append(["User ID", "User Name", "Date", "Checkins", "Checkouts", "Late", "Total Late Days"])
        
        late_count = {}
        for r in rows:
            uid = r[0]
            if int(r[4]):
                late_count[uid] = late_count.get(uid, 0) + 1
        
        for r in rows:
            uid, date, cis, cos, late = r
            member_obj = guild.get_member(int(uid)) if guild else None
            name = member_obj.display_name if member_obj else uid
            total_late = late_count.get(uid, 0)
            ws.append([uid, name, date, ", ".join(json.loads(cis or "[]")), ", ".join(json.loads(cos or "[]")), "Yes" if int(late) else "No", total_late])
    
    elif mode == "separate":
        by_user = {}
        for r in rows:
            uid, date, cis, cos, late = r
            by_user.setdefault(uid, []).append((date, cis, cos, late))

        users = list(by_user.keys())
        used_names = set()

        for idx, uid in enumerate(users):
            member_obj = guild.get_member(int(uid)) if guild else None
            raw_name = member_obj.display_name if member_obj else uid
            
            base = raw_name[:28]
            sheet_name = base
            suffix = 1
            while sheet_name in used_names:
                sheet_name = (base[:max(0, 28 - len(str(suffix)))] + f"_{suffix}")[:31]
                suffix += 1
            used_names.add(sheet_name)

            if idx == 0:
                ws = wb.active
            else:
                ws = wb.create_sheet(title=sheet_name)
            
            ws.title = sheet_name
            ws.append(["User ID", "User Name", "Date", "Checkins", "Checkouts", "Late", "Total Late Days"])
            
            total_late_days = sum(1 for d in by_user[uid] if int(d[3]))
            
            for date, cis, cos, late in by_user[uid]:
                name = member_obj.display_name if member_obj else uid
                ws.append([uid, name, date, ", ".join(json.loads(cis or "[]")), ", ".join(json.loads(cos or "[]")), "Yes" if int(late) else "No", total_late_days])

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)
    fn = f"attendance_{mode}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.xlsx"
    
    await interaction.response.send_message(
        f"✅ Attendance Export Ready",
        file=discord.File(fp=bio, filename=fn),
        ephemeral=True
    )

# ---- DAILY REPORT TASK ----

@tasks.loop(minutes=1.0)
async def daily_report_loop():
    now = now_local()
    target_h, target_m = map(int, DAILY_REPORT_TIME.split(":"))
    if now.hour == target_h and now.minute == target_m:
        guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
        if not guild:
            return
        channel = None
        for c in guild.text_channels:
            if c.permissions_for(guild.me).send_messages:
                channel = c
                break
        if not channel:
            return
        date = today_str()
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT user_id, checkins, checkouts, late FROM attendance WHERE date=?", (date,)) as cursor:
                rows = await cursor.fetchall()
        present_ids = {r[0] for r in rows}
        late_ids = {r[0] for r in rows if int(r[3])}
        hours = {}
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT user_id, checkins, checkouts FROM attendance WHERE date=?", (date,)) as cursor:
                rr = await cursor.fetchall()
                for r in rr:
                    uid = r[0]
                    cis = json.loads(r[1] or "[]")
                    cos = json.loads(r[2] or "[]")
                    hours[uid] = compute_hours_for_lists(cis, cos)
        absent_members = [m for m in guild.members if not m.bot and str(m.id) not in present_ids]
        present_list = []
        for pid in present_ids:
            member = guild.get_member(int(pid))
            name = member.display_name if member else pid
            late_mark = " (Late)" if pid in late_ids else ""
            hrs = f" — {hours.get(pid, 0):.2f} hrs" if pid in hours else ""
            present_list.append(f"• {name}{late_mark}{hrs}")
        if not present_list:
            present_text = "No one has checked in today."
        else:
            present_text = "\n".join(present_list)
        absent_text = "\n".join(f"• {m.display_name}" for m in absent_members) if absent_members else "Everyone is present."
        late_text = "\n".join(f"• {guild.get_member(int(pid)).display_name if guild.get_member(int(pid)) else pid}" for pid in late_ids) if late_ids else "No late arrivals today."
        report = (f"📅 **Daily Attendance Report — {date}**\n\n"
                  f"**Present:**\n{present_text}\n\n"
                  f"**Absent:**\n{absent_text}\n\n"
                  f"**Late:**\n{late_text}\n")
        try:
            await channel.send(report)
        except Exception:
            pass
        await asyncio.sleep(61)

# ---- ERROR HANDLING ----

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to run this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing argument. Check command usage.")
    else:
        await ctx.send(f"❌ Error: {str(error)}")

# ---- RUN ----
if __name__ == "__main__":
    if not TOKEN:
        print("Set BOT_TOKEN in .env")
    else:
        bot.run(TOKEN)

# ---- MIGRATION SCRIPT (run once) ----

async def migrate():
    async with aiosqlite.connect("attendance.sqlite") as db:
        # Create new table with id
        await db.execute("""
        CREATE TABLE attendance_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            date TEXT NOT NULL,# asyncio.run(migrate())  # Uncomment to run migration
            checkins TEXT DEFAULT '[]',            checkouts TEXT DEFAULT '[]',
            late INTEGER DEFAULT 0,
            notes TEXT DEFAULT ''
        );
        """)
        # Copy old data
        await db.execute("""
        INSERT INTO attendance_new (user_id, date, checkins, checkouts, late, notes)
        SELECT user_id, date, checkins, checkouts, late, notes FROM attendance
        """)
        # Drop old table
        await db.execute("DROP TABLE attendance")
        # Rename new table
        await db.execute("ALTER TABLE attendance_new RENAME TO attendance")
        await db.commit()


# asyncio.run(migrate())  # Uncomment to run migration
