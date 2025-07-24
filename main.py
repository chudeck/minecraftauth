import discord
from discord.ext import commands, tasks
import json
import os
import logging
import sqlite3
from datetime import datetime
import aiohttp
import asyncio
from discord.ui import Button, View, Modal, TextInput

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('minecraft_auth_bot')

# 봇 설정
BOT_TOKEN = "MTM3MDYxMjQ1OTQyOTQzMzQ3Nw.G17QsC.gMKHRJpq7kpDonaRWzGXPPlvSgol6NLzrUXfVo"  # 실제 사용 시 본인의 토큰으로 교체해야 합니다
GUILD_ID = None  # 서버 ID는 런타임에 설정됩니다
AUTH_ROLE_ID = None  # 인증 역할 ID
LOG_CHANNEL_ID = None  # 로그 채널 ID

# 데이터베이스 설정
DB_PATH = "minecraft_auth.db"

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# 데이터베이스 초기화
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        discord_id TEXT PRIMARY KEY,
        minecraft_uuid TEXT,
        minecraft_username TEXT
    )
    ''')
    conn.commit()
    conn.close()

# 설정 로드 및 저장 함수
def load_config():
    global AUTH_ROLE_ID, LOG_CHANNEL_ID
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
            AUTH_ROLE_ID = config.get('auth_role_id')
            LOG_CHANNEL_ID = config.get('log_channel_id')
    except FileNotFoundError:
        save_config()

def save_config():
    with open('config.json', 'w', encoding='utf-8') as f:
        json.dump({
            'auth_role_id': AUTH_ROLE_ID,
            'log_channel_id': LOG_CHANNEL_ID
        }, f, ensure_ascii=False, indent=4)

# 마인크래프트 인증 모달
class MinecraftAuthModal(Modal):
    def __init__(self):
        super().__init__(title="마인크래프트 계정 인증")
        
        self.username = TextInput(
            label="마인크래프트 닉네임",
            placeholder="닉네임을 입력하세요",
            required=True,
            max_length=16
        )
        self.username = TextInput(
            label="마인크래프트 닉네임",
            placeholder="닉네임을 입력하세요.",
            required=True,
            max_length=16
        )
        
        self.add_item(self.username)
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        # 닉네임 유효성 검사 및 UUID 가져오기
        username = self.username.value.strip()
        uuid, final_username = await validate_minecraft_username(username)
        
        if not uuid:
            await interaction.followup.send(f"'{username}' 마인크래프트 계정을 찾을 수 없습니다¡ 닉네임을 다시 확인해주세요.", ephemeral=True)
            return
        
        user_id = str(interaction.user.id)
        
        # 데이터베이스에 저장
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO users (discord_id, minecraft_uuid, minecraft_username) VALUES (?, ?, ?)",
            (user_id, uuid, final_username)
        )
        conn.commit()
        conn.close()
        
        # 역할 추가 및 닉네임 변경
        try:
            guild = interaction.guild
            member = guild.get_member(int(user_id))
            
            # 역할 추가
            if AUTH_ROLE_ID:
                role = guild.get_role(int(AUTH_ROLE_ID))
                if role:
                    await member.add_roles(role)
            
            # 닉네임 변경
            await member.edit(nick=final_username)
            
            await interaction.followup.send(f"'{final_username}' 마인크래프트 계정으로 인증이 완료되었습니다!", ephemeral=True)
            
            # 로그 채널에 기록
            if LOG_CHANNEL_ID:
                log_channel = bot.get_channel(int(LOG_CHANNEL_ID))
                if log_channel:
                    await log_channel.send(f"사용자 {member.mention}가 마인크래프트 계정 '{final_username}'(으)로 인증되었습니다.")
        
        except Exception as e:
            logger.error(f"인증 후 작업 오류: {e}")
            await interaction.followup.send(f"인증은 완료되었지만 역할이나 닉네임 변경 중 오류가 발생했습니다: {str(e)}", ephemeral=True)

# 인증 버튼 뷰
class MinecraftAuthView(View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="마인크래프트 인증", style=discord.ButtonStyle.primary, custom_id="minecraft_auth")
    async def minecraft_auth_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(MinecraftAuthModal())
    
    @discord.ui.button(label="닉네임 변경", style=discord.ButtonStyle.secondary, custom_id="update_nickname")
    async def update_nickname_button(self, interaction: discord.Interaction, button: Button):
        user_id = str(interaction.user.id)
        
        # 데이터베이스에서 사용자 정보 확인
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT minecraft_uuid, minecraft_username FROM users WHERE discord_id = ?", (user_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            await interaction.response.send_message("마인크래프트 인증이 완료되지 않았습니다.", ephemeral=True)
            return
        
        minecraft_uuid, old_username = result
        
        # 최신 마인크래프트 닉네임 가져오기
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.mojang.com/user/profile/{minecraft_uuid}") as resp:
                    if resp.status == 200:
                        profile_data = await resp.json()
                        new_username = profile_data.get('name')
                        
                        if new_username != old_username:
                            # 데이터베이스 업데이트
                            conn = sqlite3.connect(DB_PATH)
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE users SET minecraft_username = ? WHERE discord_id = ?",
                                (new_username, user_id)
                            )
                            conn.commit()
                            conn.close()
                            
                            # 디스코드 닉네임 변경
                            try:
                                member = interaction.guild.get_member(int(user_id))
                                await member.edit(nick=new_username)
                                await interaction.response.send_message(f"```{old_username}```'에서 ```{new_username}```(으)로 닉네임이 변경되었습니다.", ephemeral=True)
                                
                                # 로그 채널에 기록
                                if LOG_CHANNEL_ID:
                                    log_channel = bot.get_channel(int(LOG_CHANNEL_ID))
                                    if log_channel:
                                        await log_channel.send(f"사용자 {interaction.user.mention}--> '{old_username}'에서 '{new_username}'(으)로 닉네임이 변경되었습니다.")
                            except Exception as e:
                                logger.error(f"닉네임 변경 오류: {e}")
                                await interaction.response.send_message(f"닉네임 변경에 실패했습니다: {e}", ephemeral=True)
                        else:
                            await interaction.response.send_message(f"닉네임 변경을 감지하지 못했습니다.(```{new_username}```)", ephemeral=True)
                    else:
                        await interaction.response.send_message("마인크래프트 프로필을 가져오는데 실패했습니다. 나중에 다시 시도해주세요.", ephemeral=True)
        except Exception as e:
            logger.error(f"프로필 가져오기 오류: {e}")
            await interaction.response.send_message(f"프로필 가져오기 오류: {e}", ephemeral=True)

# 마인크래프트 닉네임 검증 함수
async def validate_minecraft_username(username):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.mojang.com/users/profiles/minecraft/{username}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('id'), data.get('name')  # UUID와 정확한 닉네임 반환
                else:
                    return None, None
    except Exception as e:
        logger.error(f"닉네임 검증 오류: {e}")
        return None, None

# 정기적인 닉네임 검사 및 업데이트
@tasks.loop(hours=24)
async def check_usernames():
    if not GUILD_ID:
        return
    
    guild = bot.get_guild(int(GUILD_ID))
    if not guild:
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT discord_id, minecraft_uuid, minecraft_username FROM users")
    users = cursor.fetchall()
    conn.close()
    
    for user_data in users:
        discord_id, minecraft_uuid, old_username = user_data
        
        # 마인크래프트 프로필 가져오기
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.mojang.com/user/profile/{minecraft_uuid}") as resp:
                    if resp.status == 200:
                        profile_data = await resp.json()
                        new_username = profile_data.get('name')
                        
                        # 사용자 이름이 변경되었는지 확인
                        if new_username != old_username:
                            # 데이터베이스 업데이트
                            conn = sqlite3.connect(DB_PATH)
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE users SET minecraft_username = ? WHERE discord_id = ?",
                                (new_username, discord_id)
                            )
                            conn.commit()
                            conn.close()
                            
                            # 디스코드 닉네임 변경
                            try:
                                member = guild.get_member(int(discord_id))
                                if member:
                                    await member.edit(nick=new_username)
                                    logger.info(f"사용자 {discord_id}의 닉네임이 {old_username}에서 {new_username}(으)로 업데이트되었습니다.")
                                    
                                    # 사용자에게 DM 보내기
                                    try:
                                        await member.send(f"마인크래프트 닉네임이 변경되어 디스코드 닉네임도 '{new_username}'(으)로 업데이트되었습니다.")
                                    except:
                                        pass
                                    
                                    # 로그 채널에 기록
                                    if LOG_CHANNEL_ID:
                                        log_channel = bot.get_channel(int(LOG_CHANNEL_ID))
                                        if log_channel:
                                            await log_channel.send(f"사용자 {member.mention}의 닉네임이 '{old_username}'에서 '{new_username}'(으)로 업데이트되었습니다.")
                            except Exception as e:
                                logger.error(f"닉네임 변경 오류: {e}")
        except Exception as e:
            logger.error(f"프로필 가져오기 오류: {e}")

@bot.event
async def on_ready():
    global GUILD_ID
    logger.info(f"{bot.user.name}으로 로그인했습니다.")
    
    if len(bot.guilds) > 0:
        GUILD_ID = str(bot.guilds[0].id)
        logger.info(f"서버: {bot.guilds[0].name} (ID: {GUILD_ID})")
    
    init_db()
    load_config()
    
    # 버튼 유지를 위한 영구 뷰 등록
    bot.add_view(MinecraftAuthView())
    
    # 닉네임 확인 작업 시작
    check_usernames.start()

@bot.command(name="마크인증메뉴")
async def show_auth_menu(ctx):
    view = MinecraftAuthView()
    await ctx.send("아래 마인크래프트 인증버튼을 눌러 디스코드 인증을 완료해주세요!", view=view)

@bot.command(name="마크인증")
async def setup_auth(ctx, option=None, id_value=None):
    global AUTH_ROLE_ID, LOG_CHANNEL_ID
    
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("이 명령어는 관리자만 사용할 수 있습니다.")
        return
    
    if option == "역할" and id_value:
        try:
            role = ctx.guild.get_role(int(id_value))
            if not role:
                await ctx.send("해당 ID의 역할을 찾을 수 없습니다.")
                return
            
            AUTH_ROLE_ID = id_value
            save_config()
            await ctx.send(f"인증 역할이 '{role.name}'(으)로 설정되었습니다.")
        except ValueError:
            await ctx.send("올바른 역할 ID를 입력해주세요.")
    
    elif option == "로그" and id_value:
        try:
            channel = bot.get_channel(int(id_value))
            if not channel:
                await ctx.send("해당 ID의 채널을 찾을 수 없습니다.")
                return
            
            LOG_CHANNEL_ID = id_value
            save_config()
            await ctx.send(f"로그 채널이 '{channel.name}'(으)로 설정되었습니다.")
        except ValueError:
            await ctx.send("올바른 채널 ID를 입력해주세요.")
    
    else:
        await ctx.send("사용법: !마크인증 [역할/로그] [ID]")

# 봇 실행
if __name__ == "__main__":
    bot.run(')