import os
import logging
import asyncio
from typing import Dict, Tuple, Optional, List
from datetime import datetime

import gspread
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ADMIN_IDS = [int(id_str.strip()) for id_str in os.getenv('ADMIN_IDS', '').split(',') if id_str.strip()]
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID')
GOOGLE_CREDENTIALS_FILE = os.getenv('GOOGLE_CREDENTIALS_FILE', 'credentials.json')

# Initialize bot and dispatcher
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# FSM states for handling user inputs
class FormState(StatesGroup):
    waiting_for_article = State()
    waiting_for_new_sheet_name = State()
    waiting_for_product_data = State()
    waiting_for_increase_amount = State()
    waiting_for_decrease_amount = State()

# Global cache for articles and sheets
articles_cache: Dict[str, Tuple[str, int, bool]] = {}
sheets_cache: List[str] = []

def authenticate_google_sheets():
    """Authenticate and return Google Sheets client"""
    try:
        gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        return sh
    except Exception as e:
        logging.error(f"Error authenticating with Google Sheets: {e}")
        raise

def get_sheet_structure(sheet_name: str) -> Dict:
    """Get column structure based on sheet name"""
    # Convert to lowercase for comparison, handling any possible extra spaces
    normalized_name = sheet_name.lower().strip()
    
    # Standard sheets with sizes
    if normalized_name in ['браслеты', 'кольца', 'браслетов', 'колец']:
        # Determine actual sheet name to use correct size names mapping
        if normalized_name in ['кольца', 'колец']:
            size_names_key = 'кольца'
        else:  # браслеты, браслетов
            size_names_key = 'браслеты'
        
        return {
            'photo_col': 1,  # Column A
            'name_col': 2,   # Column B
            'article_col': 3, # Column C
            'size_cols': [4, 5, 6],  # Columns D, E, F
            'has_sizes': True,
            'size_names': {size_names_key: ['50', '52', '54'] if normalized_name in ['кольца', 'колец'] else ['13-14 см', '15-17 см', '18-20 см']}
        }
    else:  # Single size products
        return {
            'photo_col': 1,  # Column A
            'name_col': 2,   # Column B
            'article_col': 3, # Column C
            'size_cols': [4],  # Column D
            'has_sizes': False,
            'size_names': {}
        }

def build_main_menu():
    """Build the main inline menu"""
    keyboard = [
        [InlineKeyboardButton(text="🔍 Поиск по категории", callback_data="category_search")],
        [InlineKeyboardButton(text="🔎 Поиск по артикулу", callback_data="article_search")],
        [InlineKeyboardButton(text="📝 Управление листами", callback_data="manage_sheets")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def build_back_button():
    """Build back button"""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]])

async def update_articles_cache():
    """Update the articles cache by reading all sheets"""
    global articles_cache, sheets_cache
    
    try:
        sh = authenticate_google_sheets()
        worksheets = sh.worksheets()
        sheets_cache = [ws.title for ws in worksheets]
        
        articles_cache.clear()
        
        for worksheet in worksheets:
            sheet_name = worksheet.title
            logging.debug(f"Processing worksheet: '{sheet_name}'")
            
            # Use case-insensitive comparison to detect sheets with sizes
            sheet_name_lower = sheet_name.lower().strip()
            has_sizes = sheet_name_lower in ['браслеты', 'кольца']
            
            # Get all values from the sheet
            rows = worksheet.get_all_values()
            
            # Skip header row if exists (first row is headers)
            start_row = 1 if len(rows) > 0 else 0
            
            for i, row in enumerate(rows[start_row:], start=start_row+1):
                if len(row) > 3:  # Ensure row has at least 4 columns (A, B, C, D)
                    article = row[2].strip() if len(row) > 2 else ''  # Column C (3rd index, 0-based)
                    if article:
                        # Store actual row number (i + 1 because rows are 1-indexed)
                        articles_cache[article] = (sheet_name, i + 1, has_sizes)
                        
            logging.debug(f"Processed {sheet_name}: has_sizes={has_sizes}")
    
    except Exception as e:
        logging.error(f"Error updating articles cache: {e}")

def find_product_by_article(article: str) -> Optional[Tuple[str, int]]:
    """Find product by article in cache with case-insensitive search"""
    article_stripped = article.strip().lower()
    
    # Direct match first
    if article in articles_cache:
        return articles_cache[article][0], articles_cache[article][1]  # sheet name, row number
    
    # Case-insensitive search
    for cached_article, (sheet_name, row_num, has_sizes) in articles_cache.items():
        if cached_article.lower() == article_stripped:
            return sheet_name, row_num
    
    # Partial match if nothing found
    for cached_article, (sheet_name, row_num, has_sizes) in articles_cache.items():
        if article_stripped in cached_article.lower() or cached_article.lower() in article_stripped:
            return sheet_name, row_num
    
    return None

def get_product_info(sheet_name: str, row_num: int) -> Optional[Dict]:
    """Get product info by sheet name and row number"""
    try:
        sh = authenticate_google_sheets()
        worksheet = sh.worksheet(sheet_name)
        
        # Adjust row_num to account for header row (we count from 2nd row in spreadsheet)
        actual_row_num = row_num  # We maintain original logic since cache stores actual row numbers
        row_data = worksheet.row_values(actual_row_num)
        
        if not row_data:
            return None
            
        structure = get_sheet_structure(sheet_name)
        
        product_info = {
            'photo_url': row_data[structure['photo_col']-1] if len(row_data) > structure['photo_col']-1 else '',
            'name': row_data[structure['name_col']-1] if len(row_data) > structure['name_col']-1 else '',
            'article': row_data[structure['article_col']-1] if len(row_data) > structure['article_col']-1 else '',
            'has_sizes': structure['has_sizes']
        }
        
        if structure['has_sizes']:
            sizes_info = []
            for i, col in enumerate(structure['size_cols']):
                size_value = row_data[col-1] if len(row_data) > col-1 else '0'
                try:
                    quantity = int(size_value) if size_value.isdigit() else 0
                except ValueError:
                    quantity = 0
                    
                # Get size names from header row if available, fallback to defaults
                size_name = f'Размер {i+1}'  # Default name
                try:
                    header_row = worksheet.row_values(1)  # Get first row (header)
                    if len(header_row) >= col and header_row[col-1].strip():
                        size_name = header_row[col-1].strip()
                        # If the header is empty or just a number, use default/predefined name
                        if not size_name or size_name.isdigit():
                            if sheet_name.lower() in structure['size_names'] and i < len(structure['size_names'][sheet_name.lower()]):
                                size_name = structure['size_names'][sheet_name.lower()][i]
                    else:
                        # Fallback to predefined names if header is empty
                        if sheet_name.lower() in structure['size_names'] and i < len(structure['size_names'][sheet_name.lower()]):
                            size_name = structure['size_names'][sheet_name.lower()][i]
                except Exception as e:
                    # Log the error for debugging
                    logging.debug(f"Error reading header for sheet {sheet_name}, col {col}: {e}")
                    # Use predefined names if header reading fails
                    if sheet_name.lower() in structure['size_names'] and i < len(structure['size_names'][sheet_name.lower()]):
                        size_name = structure['size_names'][sheet_name.lower()][i]
                
                sizes_info.append({
                    'size': size_name,
                    'quantity': quantity,
                    'col_index': col
                })
            product_info['sizes'] = sizes_info
        else:
            quantity = row_data[structure['size_cols'][0]-1] if len(row_data) > structure['size_cols'][0]-1 else '0'
            try:
                quantity = int(quantity) if quantity.isdigit() else 0
            except ValueError:
                quantity = 0
            product_info['quantity'] = quantity
            product_info['col_index'] = structure['size_cols'][0]
        
        return product_info
    except Exception as e:
        logging.error(f"Error getting product info: {e}")
        return None

def build_category_selection_menu():
    """Build category selection menu"""
    keyboard = []
    
    # Add standard categories first
    standard_categories = ['Браслеты', 'Подвески', 'Серьги', 'Кольца', 'Наборы']
    for category in standard_categories:
        if category in sheets_cache:
            keyboard.append([InlineKeyboardButton(text=category, callback_data=f"category_{category}")])
    
    # Add any additional sheets that aren't standard
    for sheet in sheets_cache:
        if sheet not in standard_categories:
            keyboard.append([InlineKeyboardButton(text=sheet, callback_data=f"category_{sheet}")])
    
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def build_products_list(sheet_name: str, offset: int = 0) -> InlineKeyboardMarkup:
    """Build products list for a given sheet with pagination"""
    try:
        sh = authenticate_google_sheets()
        worksheet = sh.worksheet(sheet_name)
        rows = worksheet.get_all_values()
        
        # Skip header row if exists
        start_row = 1 if len(rows) > 0 else 0
        
        products = []
        for i, row in enumerate(rows[start_row:], start=start_row+1):
            if len(row) > 1:  # Need at least name and article
                name = row[1] if len(row) > 1 else 'Без названия'
                article = row[2] if len(row) > 2 else 'Без артикула'
                if name.strip() or article.strip():
                    products.append({'name': name, 'article': article, 'row_num': i})
        
        keyboard = []
        # Show products for current page (10 per page)
        page_products = products[offset:offset+10]
        
        for product in page_products:
            display_name = f"{product['name']} ({product['article']})" if product['article'] != 'Без артикула' else product['name']
            safe_sheet_name = sheet_name.replace("_", "|_|")  # Custom escaping for underscores
            keyboard.append([
                InlineKeyboardButton(
                    text=display_name,
                    callback_data=f"product|{safe_sheet_name}|{product['row_num']}"
                )
            ])
        
        # Pagination buttons
        navigation_buttons = []
        if offset > 0:
            navigation_buttons.append(InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"products_page_{sheet_name}_{offset-10}"
            ))
        
        if len(products) > offset + 10:
            if navigation_buttons:
                navigation_buttons.append(InlineKeyboardButton(
                    text="➡️ Вперед",
                    callback_data=f"products_page_{sheet_name}_{offset+10}"
                ))
            else:
                navigation_buttons = [InlineKeyboardButton(
                    text="➡️ Вперед",
                    callback_data=f"products_page_{sheet_name}_{offset+10}"
                )]
        
        if navigation_buttons:
            keyboard.append(navigation_buttons)
        
        # Back buttons
        keyboard.append([
            InlineKeyboardButton(text="📋 Выбор категории", callback_data="category_search"),
            InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")
        ])
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    except Exception as e:
        logging.error(f"Error building products list: {e}")
        return build_back_button()

def build_manage_sheets_menu():
    """Build sheets management menu"""
    keyboard = [
        [InlineKeyboardButton(text="➕ Создать новый лист", callback_data="create_sheet")],
        [InlineKeyboardButton(text="🗑️ Удалить лист", callback_data="delete_sheet_list")],
        [InlineKeyboardButton(text="📋 Посмотреть все листы", callback_data="view_all_sheets")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def build_sizes_selection(product_info: Dict, sheet_name: str, row_num: int) -> InlineKeyboardMarkup:
    """Build sizes selection menu for products with multiple sizes"""
    # Replace any underscores in sheet name to prevent parsing issues
    safe_sheet_name = sheet_name.replace("_", "|_|")  # Custom escaping for underscores
    keyboard = []
    
    for size_info in product_info['sizes']:
        keyboard.append([
            InlineKeyboardButton(
                text=f"{size_info['size']}: {size_info['quantity']} шт.",
                callback_data=f"size|{safe_sheet_name}|{row_num}|{size_info['col_index']}"
            )
        ])
    
    # Instead of going back to product info, go back to product list
    safe_sheet_name = sheet_name.replace("_", "|_|")
    keyboard.append([
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"return_to_list|{safe_sheet_name}")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def build_product_card(product_info: Dict, sheet_name: str, row_num: int, col_index: Optional[int] = None) -> Tuple[str, InlineKeyboardMarkup]:
    """Build product card with photo and controls"""
    # Determine quantity and size based on product type
    if product_info['has_sizes'] and col_index is not None:
        # Find the specific size selected
        selected_size = None
        for size_info in product_info['sizes']:
            if size_info['col_index'] == col_index:
                selected_size = size_info
                break
        
        if selected_size:
            quantity = selected_size['quantity']
            size = selected_size['size']
        else:
            # Default to first size if not found
            selected_size = product_info['sizes'][0]
            quantity = selected_size['quantity']
            size = selected_size['size']
    else:
        quantity = product_info.get('quantity', 0)
        size = "единый размер" if not product_info['has_sizes'] else ""
    
    # Build product info text
    text = f"🏷️ <b>Категория:</b> {sheet_name}\n"
    text += f"📦 <b>Название:</b> {product_info['name']}\n"
    text += f"🔢 <b>Артикул:</b> {product_info['article']}\n"
    text += f"📏 <b>Размер:</b> {size}\n"
    text += f"🛒 <b>Остаток:</b> {quantity} шт."
    
    # Build keyboard with controls
    keyboard = []
    
    # Quantity control buttons
    safe_sheet_name = sheet_name.replace("_", "|_|")
    keyboard.append([
        InlineKeyboardButton(
            text="-", 
            callback_data=f"decrease|{safe_sheet_name}|{row_num}|{col_index if col_index else product_info['col_index']}|{quantity}"
        ),
        InlineKeyboardButton(
            text="+", 
            callback_data=f"increase|{safe_sheet_name}|{row_num}|{col_index if col_index else product_info['col_index']}|{quantity}"
        )
    ])
    
    # Delete product button
    safe_sheet_name = sheet_name.replace("_", "|_|")
    keyboard.append([
        InlineKeyboardButton(
            text="🗑️ Удалить товар", 
            callback_data=f"confirm_delete_product|{safe_sheet_name}|{row_num}"
        )
    ])
    
    # Back button
    safe_sheet_name = sheet_name.replace("_", "|_|")  # This is our encoded version
    if product_info['has_sizes']:
        # If product has sizes and we're viewing a specific size, go back to size selection
        if col_index is not None:
            # Pass back to the size selection screen for this product
            keyboard.append([
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"product|{safe_sheet_name}|{row_num}")
            ])
        else:
            # If we're at the product level (selecting size), go back to product list
            keyboard.append([
                InlineKeyboardButton(text="⬅️ Назад", callback_data=f"return_to_list|{safe_sheet_name}")
            ])
    else:
        # For single-size products, go back to product list
        keyboard.append([
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"return_to_list|{safe_sheet_name}")
        ])
    
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Handle /start command"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этого бота.")
        return
    
    await message.answer(
        "💎 <b>Добро пожаловать в бота управления ювелирным ассортиментом!</b>\n\n"
        "Выберите действие:",
        reply_markup=build_main_menu(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(query: CallbackQuery):
    """Return to main menu"""
    await query.message.edit_text(
        "💎 <b>Добро пожаловать в бота управления ювелирным ассортиментом!</b>\n\n"
        "Выберите действие:",
        reply_markup=build_main_menu(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "category_search")
async def cb_category_search(query: CallbackQuery):
    """Show category selection menu"""
    await query.message.edit_text(
        "📋 <b>Выберите категорию:</b>",
        reply_markup=build_category_selection_menu(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("category_"))
async def cb_select_category(query: CallbackQuery):
    """Handle category selection"""
    category = query.data[len("category_"):]
    await query.message.edit_text(
        f"📦 <b>Товары в категории '{category}':</b>",
        reply_markup=build_products_list(category, 0),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("products_page_"))
async def cb_paginate_products(query: CallbackQuery):
    """Handle product pagination"""
    parts = query.data.split("_")
    if len(parts) >= 4:
        _, _, sheet_name, offset_str = parts[0], parts[1], parts[2], parts[3]
        try:
            offset = int(offset_str)
        except ValueError:
            offset = 0
        sheet_name = sheet_name.replace("_", " ")  # Restore spaces in sheet name
        
        await query.message.edit_text(
            f"📦 <b>Товары в категории '{sheet_name}':</b>",
            reply_markup=build_products_list(sheet_name, offset),
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("product|"))
async def cb_select_product(query: CallbackQuery):
    """Handle product selection"""
    try:
        # New format: "product|sheet_name|row_num"
        parts = query.data.split("|")
        if len(parts) >= 3:
            _, sheet_name_encoded, row_num_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            
            product_info = get_product_info(sheet_name, row_num)
            if not product_info:
                await query.answer("❌ Товар больше не существует", show_alert=True)
                return
            
            if product_info['has_sizes']:
                # Show size selection
                new_text = f"📏 <b>Выберите размер для '{product_info['name']}':</b>\n\n<i>Время: {datetime.now().strftime('%H:%M:%S')}</i>"
                try:
                    await query.message.edit_text(
                        new_text,
                        reply_markup=build_sizes_selection(product_info, sheet_name, row_num),
                        parse_mode="HTML"
                    )
                except Exception:
                    # If message hasn't changed, ignore the error
                    await query.answer("Выберите размер", show_alert=False)
            else:
                # Show product card directly
                text, keyboard = build_product_card(product_info, sheet_name, row_num)
                # Add timestamp to ensure the message is always different to avoid "message is not modified" error
                text_with_time = f"{text}\n\n<i>Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>"
                
                media = None
                if product_info['photo_url']:
                    try:
                        media = InputMediaPhoto(media=product_info['photo_url'], caption=text_with_time, parse_mode="HTML")
                        await query.message.edit_media(media=media, reply_markup=keyboard)
                    except Exception:
                        # If photo sending fails, send text message
                        await query.message.edit_text(text=text_with_time, reply_markup=keyboard, parse_mode="HTML")
                else:
                    await query.message.edit_text(text=text_with_time, reply_markup=keyboard, parse_mode="HTML")
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_select_product: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("size|"))
async def cb_select_size(query: CallbackQuery):
    """Handle size selection for products with multiple sizes"""
    try:
        # New format: "size|sheet_name|row_num|col_index"
        parts = query.data.split("|")
        if len(parts) >= 4:
            _, sheet_name_encoded, row_num_str, col_index_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            col_index = int(col_index_str)
            
            product_info = get_product_info(sheet_name, row_num)
            if not product_info:
                await query.answer("❌ Товар больше не существует", show_alert=True)
                return
            
            text, keyboard = build_product_card(product_info, sheet_name, row_num, col_index)
            # Add timestamp to ensure the message is always different to avoid "message is not modified" error
            text_with_time = f"{text}\n\n<i>Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>"
            
            media = None
            if product_info['photo_url']:
                try:
                    media = InputMediaPhoto(media=product_info['photo_url'], caption=text_with_time, parse_mode="HTML")
                    await query.message.edit_media(media=media, reply_markup=keyboard)
                except Exception:
                    # If photo sending fails, send text message
                    await query.message.edit_text(text=text_with_time, reply_markup=keyboard, parse_mode="HTML")
            else:
                await query.message.edit_text(text=text_with_time, reply_markup=keyboard, parse_mode="HTML")
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_select_size: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

@dp.callback_query(F.data == "article_search")
async def cb_article_search(query: CallbackQuery, state: FSMContext):
    """Prompt user to enter an article number"""
    await query.message.edit_text(
        "Введите артикул товара для поиска:",
        reply_markup=build_back_button()
    )
    await state.set_state(FormState.waiting_for_article)
    await query.answer("Введите артикул товара для поиска")

@dp.message(Command("search_article"))
async def cmd_search_article(message: Message, state: FSMContext):
    """Start article search process"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return
    
    await message.answer("Введите артикул товара для поиска:")
    await state.set_state(FormState.waiting_for_article)

@dp.message(FormState.waiting_for_article)
async def process_article_input(message: Message, state: FSMContext):
    """Process the entered article"""
    article = message.text.strip()
    
    try:
        # Find product by article in cache first
        result = find_product_by_article(article)
        if result:
            sheet_name, row_num = result
            product_info = get_product_info(sheet_name, row_num)
            
            if not product_info:
                await message.answer("❌ Товар больше не существует в таблице.")
                await state.clear()
                return
            
            if product_info['has_sizes']:
                # Show size selection
                await message.answer(
                    f"📏 <b>Выберите размер для '{product_info['name']}':</b>",
                    reply_markup=build_sizes_selection(product_info, sheet_name, row_num),
                    parse_mode="HTML"
                )
            else:
                # Show product card directly
                text, keyboard = build_product_card(product_info, sheet_name, row_num)
                
                if product_info['photo_url']:
                    try:
                        await message.answer_photo(
                            photo=product_info['photo_url'],
                            caption=text,
                            reply_markup=keyboard,
                            parse_mode="HTML"
                        )
                    except Exception:
                        # If photo sending fails, send text message
                        await message.answer(text=text, reply_markup=keyboard, parse_mode="HTML")
                else:
                    await message.answer(text=text, reply_markup=keyboard, parse_mode="HTML")
        else:
            # Try to find the article with case-insensitive search in cache
            article_lower = article.lower()
            found_in_cache = False
            for cached_article, (sheet_name, row_num, has_sizes) in articles_cache.items():
                if cached_article.lower() == article_lower:
                    product_info = get_product_info(sheet_name, row_num)
                    if product_info:
                        found_in_cache = True
                        if product_info['has_sizes']:
                            await message.answer(
                                f"📏 <b>Выберите размер для '{product_info['name']}':</b>",
                                reply_markup=build_sizes_selection(product_info, sheet_name, row_num),
                                parse_mode="HTML"
                            )
                        else:
                            text, keyboard = build_product_card(product_info, sheet_name, row_num)
                            
                            if product_info['photo_url']:
                                try:
                                    await message.answer_photo(
                                        photo=product_info['photo_url'],
                                        caption=text,
                                        reply_markup=keyboard,
                                        parse_mode="HTML"
                                    )
                                except Exception:
                                    await message.answer(text=text, reply_markup=keyboard, parse_mode="HTML")
                            else:
                                await message.answer(text=text, reply_markup=keyboard, parse_mode="HTML")
                    break
            
            if not found_in_cache:
                # If still not found, suggest refreshing cache
                await message.answer(f"❌ Товар с артикулом '{article}' не найден. Попробуйте обновить кэш командой /sync")
        
        await state.clear()
    except Exception as e:
        logging.error(f"Error processing article input: {e}")
        await message.answer(f"❌ Произошла ошибка при поиске товара: {str(e)}")
        await state.clear()

@dp.callback_query(F.data == "manage_sheets")
async def cb_manage_sheets(query: CallbackQuery):
    """Show sheets management menu"""
    await query.message.edit_text(
        "📋 <b>Управление листами:</b>",
        reply_markup=build_manage_sheets_menu(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "create_sheet")
async def cb_create_sheet(query: CallbackQuery):
    """Prompt to enter new sheet name"""
    await query.message.edit_text(
        "Введите название нового листа:",
        reply_markup=build_back_button()
    )
    await query.message.answer("⏳ Ожидаем ввод названия листа...")
    # In a real implementation, we would transition to FSM state here
    # For now, we'll just show a message
    await query.answer("Для создания листа используйте команду /create_sheet")

@dp.message(Command("create_sheet"))
async def cmd_create_sheet(message: Message, state: FSMContext):
    """Start sheet creation process"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return
    
    await message.answer("Введите название нового листа:")
    await state.set_state(FormState.waiting_for_new_sheet_name)

@dp.message(FormState.waiting_for_new_sheet_name)
async def process_new_sheet_name(message: Message, state: FSMContext):
    """Process new sheet name and create sheet"""
    sheet_name = message.text.strip()
    
    try:
        sh = authenticate_google_sheets()
        
        # Check if sheet already exists
        existing_worksheets = sh.worksheets()
        if any(ws.title == sheet_name for ws in existing_worksheets):
            await message.answer(f"❌ Лист с названием '{sheet_name}' уже существует.")
            await state.clear()
            return
        
        # Create new worksheet
        worksheet = sh.add_worksheet(title=sheet_name, rows="1000", cols="10")
        
        # Add headers based on sheet type
        structure = get_sheet_structure(sheet_name)
        if structure['has_sizes']:
            if sheet_name.lower() in ['браслеты', 'кольца']:
                headers = ['URL фото', 'Название', 'Артикул']
                if sheet_name.lower() == 'браслеты':
                    headers.extend(['Количество 16 (13-14см)', 'Количество 18 (15-17см)', 'Количество 20'])
                elif sheet_name.lower() == 'кольца':
                    headers.extend(['Количество 50', 'Количество 52', 'Количество 54'])
                
                worksheet.update('A1:' + chr(64+len(headers)) + '1', [headers])
        else:
            headers = ['URL фото', 'Название', 'Артикул', 'Количество']
            worksheet.update('A1:D1', [headers])
        
        await message.answer(f"✅ Лист '{sheet_name}' успешно создан с заголовками.")
        await update_articles_cache()  # Refresh cache
        await state.clear()
    except Exception as e:
        logging.error(f"Error creating sheet: {e}")
        await message.answer(f"❌ Ошибка при создании листа: {str(e)}")
        await state.clear()

@dp.callback_query(F.data.startswith("increase_"))
async def cb_increase_quantity(query: CallbackQuery):
    """Handle increasing quantity"""
    parts = query.data.split("_")
    if len(parts) >= 5:
        _, sheet_name, row_num_str, col_index_str, current_qty_str = parts[:5]
        row_num = int(row_num_str)
        col_index = int(col_index_str)
        current_qty = int(current_qty_str)
        
        safe_sheet_name = sheet_name.replace("_", "|_|")
        # Create confirmation buttons
        confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ +1", callback_data=f"confirm_increase|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|1")],
            [InlineKeyboardButton(text="➕ +5", callback_data=f"confirm_increase|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|5")],
            [InlineKeyboardButton(text="➕ +10", callback_data=f"confirm_increase|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|10")],
            [InlineKeyboardButton(text="🔢 Ввести свое", callback_data=f"input_increase|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_change|{safe_sheet_name}|{row_num}|{col_index}")]
        ])
        
        await query.message.edit_text(
            f"📦 <b>Текущее количество:</b> {current_qty} шт.\n\n"
            f"Выберите, на сколько увеличить количество:",
            reply_markup=confirm_keyboard,
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("confirm_increase|"))
async def cb_confirm_increase_quantity(query: CallbackQuery):
    """Confirm increasing quantity"""
    try:
        parts = query.data.split("|")
        if len(parts) >= 6:
            _, sheet_name_encoded, row_num_str, col_index_str, current_qty_str, increase_amount_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            col_index = int(col_index_str)
            current_qty = int(current_qty_str)
            increase_amount = int(increase_amount_str)
            
            new_qty = current_qty + increase_amount
            
            # Update the cell in Google Sheets
            try:
                sh = authenticate_google_sheets()
                worksheet = sh.worksheet(sheet_name)
                
                # Update the cell at (row_num, col_index)
                worksheet.update_cell(row_num, col_index, str(new_qty))
                
                # Refresh cache
                await update_articles_cache()
                
                # Get updated product info
                product_info = get_product_info(sheet_name, row_num)
                if product_info:
                    text, keyboard = build_product_card(product_info, sheet_name, row_num, col_index)
                    # Add timestamp to ensure the message is always different to avoid "message is not modified" error
                    text_with_time = f"{text}\n\n<i>Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>"
                    
                    media = None
                    if product_info['photo_url']:
                        try:
                            media = InputMediaPhoto(media=product_info['photo_url'], caption=text_with_time, parse_mode="HTML")
                            await query.message.edit_media(media=media, reply_markup=keyboard)
                        except Exception:
                            # If photo sending fails, send text message
                            await query.message.edit_text(text=text_with_time, reply_markup=keyboard, parse_mode="HTML")
                    else:
                        await query.message.edit_text(text=text_with_time, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Error updating quantity: {e}")
                await query.answer(f"❌ Ошибка обновления: {str(e)}", show_alert=True)
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_confirm_increase_quantity: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("decrease_"))
async def cb_decrease_quantity(query: CallbackQuery):
    """Handle decreasing quantity"""
    parts = query.data.split("_")
    if len(parts) >= 5:
        _, sheet_name, row_num_str, col_index_str, current_qty_str = parts[:5]
        row_num = int(row_num_str)
        col_index = int(col_index_str)
        current_qty = int(current_qty_str)
        
        safe_sheet_name = sheet_name.replace("_", "|_|")
        # Create confirmation buttons
        confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➖ -1", callback_data=f"confirm_decrease|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|1")],
            [InlineKeyboardButton(text="➖ -5", callback_data=f"confirm_decrease|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|5")],
            [InlineKeyboardButton(text="➖ -10", callback_data=f"confirm_decrease|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|10")],
            [InlineKeyboardButton(text="🔢 Ввести свое", callback_data=f"input_decrease|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_change|{safe_sheet_name}|{row_num}|{col_index}")]
        ])
        
        await query.message.edit_text(
            f"📦 <b>Текущее количество:</b> {current_qty} шт.\n\n"
            f"Выберите, на сколько уменьшить количество:",
            reply_markup=confirm_keyboard,
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("confirm_decrease|"))
async def cb_confirm_decrease_quantity(query: CallbackQuery):
    """Confirm decreasing quantity"""
    try:
        parts = query.data.split("|")
        if len(parts) >= 6:
            _, sheet_name_encoded, row_num_str, col_index_str, current_qty_str, decrease_amount_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            col_index = int(col_index_str)
            current_qty = int(current_qty_str)
            decrease_amount = int(decrease_amount_str)
            
            # Don't allow negative quantities
            new_qty = max(0, current_qty - decrease_amount)
            
            # Update the cell in Google Sheets
            try:
                sh = authenticate_google_sheets()
                worksheet = sh.worksheet(sheet_name)
                
                # Update the cell at (row_num, col_index)
                worksheet.update_cell(row_num, col_index, str(new_qty))
                
                # Refresh cache
                await update_articles_cache()
                
                # Get updated product info
                product_info = get_product_info(sheet_name, row_num)
                if product_info:
                    text, keyboard = build_product_card(product_info, sheet_name, row_num, col_index)
                    # Add timestamp to ensure the message is always different to avoid "message is not modified" error
                    text_with_time = f"{text}\n\n<i>Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>"
                    
                    media = None
                    if product_info['photo_url']:
                        try:
                            media = InputMediaPhoto(media=product_info['photo_url'], caption=text_with_time, parse_mode="HTML")
                            await query.message.edit_media(media=media, reply_markup=keyboard)
                        except Exception:
                            # If photo sending fails, send text message
                            await query.message.edit_text(text=text_with_time, reply_markup=keyboard, parse_mode="HTML")
                    else:
                        await query.message.edit_text(text=text_with_time, reply_markup=keyboard, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Error updating quantity: {e}")
                await query.answer(f"❌ Ошибка обновления: {str(e)}", show_alert=True)
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_confirm_decrease_quantity: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("confirm_delete_product_"))
async def cb_confirm_delete_product(query: CallbackQuery):
    """Confirm product deletion"""
    try:
        parts = query.data.split("|")
        if len(parts) >= 3:
            _, sheet_name_encoded, row_num_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            
            product_info = get_product_info(sheet_name, row_num)
            if not product_info:
                await query.answer("❌ Товар уже удален или не существует.", show_alert=True)
                return
            
            # Escape underscores in sheet name for consistency
            safe_sheet_name = sheet_name.replace("_", "|_|")
            confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Да, удалить", callback_data=f"delete_product|{safe_sheet_name}|{row_num}")],
                [InlineKeyboardButton(text="✅ Нет, отмена", callback_data=f"product|{safe_sheet_name}|{row_num}")]
            ])
            
            await query.message.edit_text(
                f"⚠️ <b>Вы уверены, что хотите удалить следующий товар?</b>\n\n"
                f"📦 <b>Название:</b> {product_info['name']}\n"
                f"🔢 <b>Артикул:</b> {product_info['article']}\n"
                f"🏷️ <b>Категория:</b> {sheet_name}",
                reply_markup=confirm_keyboard,
                parse_mode="HTML"
            )
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_confirm_delete_product: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("delete_product|"))
async def cb_delete_product(query: CallbackQuery):
    """Delete product from sheet"""
    try:
        parts = query.data.split("|")
        if len(parts) >= 3:
            _, sheet_name_encoded, row_num_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            
            try:
                sh = authenticate_google_sheets()
                worksheet = sh.worksheet(sheet_name)
                
                # Clear the row (set all cells in the row to empty)
                # First, get the number of columns to clear the whole row
                num_cols = worksheet.col_count
                range_to_clear = f'A{row_num}:{chr(64+num_cols)}{row_num}'
                worksheet.batch_clear([range_to_clear])
                
                await query.answer("✅ Товар успешно удален!", show_alert=True)
                
                # Go back to the products list
                await query.message.edit_text(
                    f"📦 <b>Товар удален. Товары в категории '{sheet_name}':</b>",
                    reply_markup=build_products_list(sheet_name, 0),
                    parse_mode="HTML"
                )
                
                # Refresh cache
                await update_articles_cache()
            except Exception as e:
                logging.error(f"Error deleting product: {e}")
                await query.answer(f"❌ Ошибка при удалении товара: {str(e)}", show_alert=True)
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_delete_product: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("return_to_list|"))
async def cb_return_to_list(query: CallbackQuery):
    """Return to product list from product card"""
    try:
        parts = query.data.split("|")
        if len(parts) >= 2:
            _, sheet_name_encoded = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            
            await query.message.edit_text(
                f"📦 <b>Товары в категории '{sheet_name}':</b>",
                reply_markup=build_products_list(sheet_name, 0),
                parse_mode="HTML"
            )
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_return_to_list: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

@dp.message(Command("sync"))
async def cmd_sync(message: Message):
    """Manually sync the articles cache"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return
    
    try:
        await update_articles_cache()
        await message.answer(f"✅ Кэш обновлен! Найдено {len(articles_cache)} артикулов в {len(sheets_cache)} листах.")
    except Exception as e:
        logging.error(f"Error syncing cache: {e}")
        await message.answer(f"❌ Ошибка обновления кэша: {str(e)}")

@dp.callback_query(F.data.startswith("input_increase|"))
async def cb_input_increase_quantity(query: CallbackQuery, state: FSMContext):
    """Prompt for manual input of increase amount"""
    try:
        parts = query.data.split("|")
        if len(parts) >= 5:
            _, sheet_name_encoded, row_num_str, col_index_str, current_qty_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            col_index = int(col_index_str)
            current_qty = int(current_qty_str)
            
            await query.message.edit_text(
                f"📦 <b>Текущее количество:</b> {current_qty} шт.\n\n"
                f"Введите число, на которое нужно увеличить количество:",
                parse_mode="HTML"
            )
            
            # Save current context for later use
            await state.update_data(
                sheet_name=sheet_name,
                row_num=row_num,
                col_index=col_index,
                current_qty=current_qty,
                action="increase"
            )
            await state.set_state(FormState.waiting_for_increase_amount)
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_input_increase_quantity: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

@dp.message(FormState.waiting_for_increase_amount)
async def process_manual_increase(message: Message, state: FSMContext):
    """Process manual increase amount input"""
    try:
        amount = int(message.text)
        if amount <= 0:
            await message.answer("❌ Введите положительное число.")
            return
            
        data = await state.get_data()
        sheet_name = data['sheet_name']
        row_num = data['row_num']
        col_index = data['col_index']
        current_qty = data['current_qty']
        
        new_qty = current_qty + amount
        
        # Update the cell in Google Sheets
        sh = authenticate_google_sheets()
        worksheet = sh.worksheet(sheet_name)
        
        # Update the cell at (row_num, col_index)
        worksheet.update_cell(row_num, col_index, str(new_qty))
        
        # Refresh cache
        await update_articles_cache()
        
        # Get updated product info
        product_info = get_product_info(sheet_name, row_num)
        if product_info:
            text, keyboard = build_product_card(product_info, sheet_name, row_num, col_index)
            
            if product_info['photo_url']:
                try:
                    await message.answer_photo(
                        photo=product_info['photo_url'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                except Exception:
                    # If photo sending fails, send text message
                    await message.answer(text=text, reply_markup=keyboard, parse_mode="HTML")
            else:
                await message.answer(text=text, reply_markup=keyboard, parse_mode="HTML")
        
        await state.clear()
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число.")
    except Exception as e:
        logging.error(f"Error updating quantity: {e}")
        await message.answer(f"❌ Ошибка обновления: {str(e)}")
        await state.clear()

@dp.callback_query(F.data.startswith("input_decrease|"))
async def cb_input_decrease_quantity(query: CallbackQuery, state: FSMContext):
    """Prompt for manual input of decrease amount"""
    try:
        parts = query.data.split("|")
        if len(parts) >= 5:
            _, sheet_name_encoded, row_num_str, col_index_str, current_qty_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            col_index = int(col_index_str)
            current_qty = int(current_qty_str)
            
            await query.message.edit_text(
                f"📦 <b>Текущее количество:</b> {current_qty} шт.\n\n"
                f"Введите число, на которое нужно уменьшить количество:",
                parse_mode="HTML"
            )
            
            # Save current context for later use
            await state.update_data(
                sheet_name=sheet_name,
                row_num=row_num,
                col_index=col_index,
                current_qty=current_qty,
                action="decrease"
            )
            await state.set_state(FormState.waiting_for_decrease_amount)
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_input_decrease_quantity: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

@dp.message(FormState.waiting_for_decrease_amount)
async def process_manual_decrease(message: Message, state: FSMContext):
    """Process manual decrease amount input"""
    try:
        amount = int(message.text)
        if amount <= 0:
            await message.answer("❌ Введите положительное число.")
            return
            
        data = await state.get_data()
        sheet_name = data['sheet_name']
        row_num = data['row_num']
        col_index = data['col_index']
        current_qty = data['current_qty']
        
        # Don't allow negative quantities
        new_qty = max(0, current_qty - amount)
        
        # Update the cell in Google Sheets
        sh = authenticate_google_sheets()
        worksheet = sh.worksheet(sheet_name)
        
        # Update the cell at (row_num, col_index)
        worksheet.update_cell(row_num, col_index, str(new_qty))
        
        # Refresh cache
        await update_articles_cache()
        
        # Get updated product info
        product_info = get_product_info(sheet_name, row_num)
        if product_info:
            text, keyboard = build_product_card(product_info, sheet_name, row_num, col_index)
            
            if product_info['photo_url']:
                try:
                    await message.answer_photo(
                        photo=product_info['photo_url'],
                        caption=text,
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                except Exception:
                    # If photo sending fails, send text message
                    await message.answer(text=text, reply_markup=keyboard, parse_mode="HTML")
            else:
                await message.answer(text=text, reply_markup=keyboard, parse_mode="HTML")
        
        await state.clear()
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректное число.")
    except Exception as e:
        logging.error(f"Error updating quantity: {e}")
        await message.answer(f"❌ Ошибка обновления: {str(e)}")
        await state.clear()

@dp.callback_query(F.data.startswith("cancel_change|"))
async def cb_cancel_change(query: CallbackQuery):
    """Cancel the change and return to the product card"""
    try:
        parts = query.data.split("|")
        if len(parts) >= 4:
            _, sheet_name_encoded, row_num_str, col_index_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            col_index = int(col_index_str)
            
            product_info = get_product_info(sheet_name, row_num)
            if product_info:
                text, keyboard = build_product_card(product_info, sheet_name, row_num, col_index)
                # Add timestamp to ensure the message is always different to avoid "message is not modified" error
                text_with_time = f"{text}\n\n<i>Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>"
                
                media = None
                if product_info['photo_url']:
                    try:
                        media = InputMediaPhoto(media=product_info['photo_url'], caption=text_with_time, parse_mode="HTML")
                        await query.message.edit_media(media=media, reply_markup=keyboard)
                    except Exception:
                        # If photo sending fails, send text message
                        await query.message.edit_text(text=text_with_time, reply_markup=keyboard, parse_mode="HTML")
                else:
                    await query.message.edit_text(text=text_with_time, reply_markup=keyboard, parse_mode="HTML")
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_cancel_change: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("increase|"))
async def cb_increase_quantity(query: CallbackQuery):
    """Handle increasing quantity"""
    try:
        parts = query.data.split("|")
        if len(parts) >= 5:
            _, sheet_name_encoded, row_num_str, col_index_str, current_qty_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            col_index = int(col_index_str)
            current_qty = int(current_qty_str)
            
            # Create confirmation buttons
            confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ +1", callback_data=f"confirm_increase|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|1")],
                [InlineKeyboardButton(text="➕ +5", callback_data=f"confirm_increase|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|5")],
                [InlineKeyboardButton(text="➕ +10", callback_data=f"confirm_increase|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|10")],
                [InlineKeyboardButton(text="🔢 Ввести свое", callback_data=f"input_increase|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_change|{sheet_name_encoded}|{row_num}|{col_index}")]
            ])
            
            await query.message.edit_text(
                f"📦 <b>Текущее количество:</b> {current_qty} шт.\n\n"
                f"Выберите, на сколько увеличить количество:",
                reply_markup=confirm_keyboard,
                parse_mode="HTML"
            )
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_increase_quantity: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("decrease|"))
async def cb_decrease_quantity(query: CallbackQuery):
    """Handle decreasing quantity"""
    try:
        parts = query.data.split("|")
        if len(parts) >= 5:
            _, sheet_name_encoded, row_num_str, col_index_str, current_qty_str = parts
            # Restore underscores in sheet name
            sheet_name = sheet_name_encoded.replace("|_|", "_")
            row_num = int(row_num_str)
            col_index = int(col_index_str)
            current_qty = int(current_qty_str)
            
            # Create confirmation buttons
            confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➖ -1", callback_data=f"confirm_decrease|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|1")],
                [InlineKeyboardButton(text="➖ -5", callback_data=f"confirm_decrease|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|5")],
                [InlineKeyboardButton(text="➖ -10", callback_data=f"confirm_decrease|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|10")],
                [InlineKeyboardButton(text="🔢 Ввести свое", callback_data=f"input_decrease|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_change|{sheet_name_encoded}|{row_num}|{col_index}")]
            ])
            
            await query.message.edit_text(
                f"📦 <b>Текущее количество:</b> {current_qty} шт.\n\n"
                f"Выберите, на сколько уменьшить количество:",
                reply_markup=confirm_keyboard,
                parse_mode="HTML"
            )
        else:
            await query.answer("❌ Неверный формат данных", show_alert=True)
    except ValueError:
        await query.answer("❌ Ошибка обработки данных", show_alert=True)
    except Exception as e:
        logging.error(f"Error in cb_decrease_quantity: {e}")
        await query.answer("❌ Произошла ошибка", show_alert=True)

async def main():
    """Main function to run the bot"""
    # Initialize the articles cache when starting
    try:
        await update_articles_cache()
        logging.info("Articles cache initialized successfully")
    except Exception as e:
        logging.error(f"Error initializing articles cache: {e}")
        return

    logging.info("Starting bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
