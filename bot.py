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
    waiting_for_new_post_name = State()
    waiting_for_new_post_text = State()
    waiting_for_edit_post_text = State()


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
            'name_col': 2,  # Column B
            'article_col': 3,  # Column C
            'size_cols': [4, 5, 6],  # Columns D, E, F
            'has_sizes': True,
            'size_names': {
                size_names_key: ['50', '52', '54'] if normalized_name in ['кольца', 'колец'] else ['13-14 см',
                                                                                                   '15-17 см',
                                                                                                   '18-20 см']}
        }
    else:  # Single size products
        return {
            'photo_col': 1,  # Column A
            'name_col': 2,  # Column B
            'article_col': 3,  # Column C
            'size_cols': [4],  # Column D
            'has_sizes': False,
            'size_names': {}
        }


def build_main_menu():
    """Build the main inline menu"""
    keyboard = [
        [InlineKeyboardButton(text="🔍 Поиск по категории", callback_data="category_search")],
        [InlineKeyboardButton(text="🔎 Поиск по артикулу", callback_data="article_search")],
        [InlineKeyboardButton(text="📝 Управление листами", callback_data="manage_sheets")],
        [InlineKeyboardButton(text="📢 Спам-бот", callback_data="spam_panel")]
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

            # Skip header row if exists (first row is headers) - we start from row 2 (index 1 in 0-based indexing)
            # Google Sheets rows are 1-indexed, so first data row is row 2
            for i, row in enumerate(rows[1:], start=2):  # Start from row 2 (second row)
                if len(row) > 2:  # Ensure row has at least 3 columns (A, B, C)
                    article = row[2].strip() if len(row) > 2 else ''  # Column C (index 2, 0-based)
                    if article:
                        # Store actual row number (i, which starts from 2)
                        articles_cache[article] = (sheet_name, i, has_sizes)
                        logging.debug(f"Cached article '{article}' from row {i} in sheet '{sheet_name}'")

            logging.debug(f"Processed {sheet_name}: has_sizes={has_sizes}, {len(articles_cache)} articles in cache")

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
            'photo_url': row_data[structure['photo_col'] - 1] if len(row_data) > structure['photo_col'] - 1 else '',
            'name': row_data[structure['name_col'] - 1] if len(row_data) > structure['name_col'] - 1 else '',
            'article': row_data[structure['article_col'] - 1] if len(row_data) > structure['article_col'] - 1 else '',
            'has_sizes': structure['has_sizes']
        }

        if structure['has_sizes']:
            sizes_info = []
            for i, col in enumerate(structure['size_cols']):
                size_value = row_data[col - 1] if len(row_data) > col - 1 else '0'
                try:
                    quantity = int(size_value) if size_value.isdigit() else 0
                except ValueError:
                    quantity = 0

                # Get size names from header row if available, fallback to defaults
                size_name = f'Размер {i + 1}'  # Default name
                try:
                    header_row = worksheet.row_values(1)  # Get first row (header)
                    if len(header_row) >= col and header_row[col - 1].strip():
                        size_name = header_row[col - 1].strip()
                        # If the header is empty or just a number, use default/predefined name
                        if not size_name or size_name.isdigit():
                            if sheet_name.lower() in structure['size_names'] and i < len(
                                    structure['size_names'][sheet_name.lower()]):
                                size_name = structure['size_names'][sheet_name.lower()][i]
                    else:
                        # Fallback to predefined names if header is empty
                        if sheet_name.lower() in structure['size_names'] and i < len(
                                structure['size_names'][sheet_name.lower()]):
                            size_name = structure['size_names'][sheet_name.lower()][i]
                except Exception as e:
                    # Log the error for debugging
                    logging.debug(f"Error reading header for sheet {sheet_name}, col {col}: {e}")
                    # Use predefined names if header reading fails
                    if sheet_name.lower() in structure['size_names'] and i < len(
                            structure['size_names'][sheet_name.lower()]):
                        size_name = structure['size_names'][sheet_name.lower()][i]

                sizes_info.append({
                    'size': size_name,
                    'quantity': quantity,
                    'col_index': col
                })
            product_info['sizes'] = sizes_info
        else:
            quantity = row_data[structure['size_cols'][0] - 1] if len(row_data) > structure['size_cols'][0] - 1 else '0'
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
        for i, row in enumerate(rows[start_row:], start=start_row + 1):
            if len(row) > 1:  # Need at least name and article
                name = row[1] if len(row) > 1 else 'Без названия'
                article = row[2] if len(row) > 2 else 'Без артикула'
                if name.strip() or article.strip():
                    products.append({'name': name, 'article': article, 'row_num': i})

        keyboard = []
        # Show products for current page (10 per page)
        page_products = products[offset:offset + 10]

        for product in page_products:
            display_name = f"{product['name']} ({product['article']})" if product['article'] != 'Без артикула' else \
                product['name']
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
                callback_data=f"products_page_{sheet_name}_{offset - 10}"
            ))

        if len(products) > offset + 10:
            if navigation_buttons:
                navigation_buttons.append(InlineKeyboardButton(
                    text="➡️ Вперед",
                    callback_data=f"products_page_{sheet_name}_{offset + 10}"
                ))
            else:
                navigation_buttons = [InlineKeyboardButton(
                    text="➡️ Вперед",
                    callback_data=f"products_page_{sheet_name}_{offset + 10}"
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


def build_product_card(product_info: Dict, sheet_name: str, row_num: int, col_index: Optional[int] = None) -> Tuple[
    str, InlineKeyboardMarkup]:
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


@dp.message(Command("spam"))
async def cmd_spam_shortcut(message: Message):
    """Shortcut to spam panel"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return

    status = "🟢 Активен" if spam_manager.is_running else "🔴 Остановлен"
    active_posts = len([f for f in os.listdir("spam_bot/texts") if f.endswith('.txt')]) if os.path.exists(
        "spam_bot/texts") else 0

    keyboard = [
        [InlineKeyboardButton(text="🟢 Запустить спам", callback_data="spam_start")],
        [InlineKeyboardButton(text="🔴 Остановить спам", callback_data="spam_stop")],
        [InlineKeyboardButton(text="📝 Управление постами", callback_data="spam_manage_posts")],
        [InlineKeyboardButton(text="📜 Логи", callback_data="spam_view_logs")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="spam_stats")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]
    ]

    await message.answer(
        "<b>📢 Панель управления спам-ботом</b>\n\n"
        f"Статус: {status}\n"
        f"Активных постов: {active_posts}\n"
        f"Последние события: {len(spam_manager.logs)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
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
                        media = InputMediaPhoto(media=product_info['photo_url'], caption=text_with_time,
                                                parse_mode="HTML")
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
                await message.answer(
                    f"❌ Товар с артикулом '{article}' не найден. Попробуйте обновить кэш командой /sync")

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

                worksheet.update('A1:' + chr(64 + len(headers)) + '1', [headers])
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
            [InlineKeyboardButton(text="➕ +1",
                                  callback_data=f"confirm_increase|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|1")],
            [InlineKeyboardButton(text="➕ +5",
                                  callback_data=f"confirm_increase|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|5")],
            [InlineKeyboardButton(text="➕ +10",
                                  callback_data=f"confirm_increase|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|10")],
            [InlineKeyboardButton(text="🔢 Ввести свое",
                                  callback_data=f"input_increase|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}")],
            [InlineKeyboardButton(text="❌ Отмена",
                                  callback_data=f"cancel_change|{safe_sheet_name}|{row_num}|{col_index}")]
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
                            media = InputMediaPhoto(media=product_info['photo_url'], caption=text_with_time,
                                                    parse_mode="HTML")
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
            [InlineKeyboardButton(text="➖ -1",
                                  callback_data=f"confirm_decrease|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|1")],
            [InlineKeyboardButton(text="➖ -5",
                                  callback_data=f"confirm_decrease|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|5")],
            [InlineKeyboardButton(text="➖ -10",
                                  callback_data=f"confirm_decrease|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}|10")],
            [InlineKeyboardButton(text="🔢 Ввести свое",
                                  callback_data=f"input_decrease|{safe_sheet_name}|{row_num}|{col_index}|{current_qty}")],
            [InlineKeyboardButton(text="❌ Отмена",
                                  callback_data=f"cancel_change|{safe_sheet_name}|{row_num}|{col_index}")]
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
                            media = InputMediaPhoto(media=product_info['photo_url'], caption=text_with_time,
                                                    parse_mode="HTML")
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
                [InlineKeyboardButton(text="❌ Да, удалить",
                                      callback_data=f"delete_product|{safe_sheet_name}|{row_num}")],
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
                range_to_clear = f'A{row_num}:{chr(64 + num_cols)}{row_num}'
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


# Global variables for spam bot management
spam_bot_process = None
spam_bot_logs = []
MAX_LOG_ENTRIES = 50


class SpamBotManager:
    def __init__(self):
        self.process = None
        self.is_running = False
        self.logs = []
        self.current_post_index = 0
        self.photo_upload_states = {}  # Track photo upload for specific user-post pairs

    def add_log(self, message):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"[{timestamp}] {message}"
        self.logs.append(log_entry)
        if len(self.logs) > MAX_LOG_ENTRIES:
            self.logs.pop(0)

    def get_logs(self, count=10):
        return self.logs[-count:] if len(self.logs) >= count else self.logs[:]

    def clear_logs(self):
        self.logs.clear()

    async def set_photo_upload_state(self, user_id, post_name):
        """Set state to wait for photo upload for specific post"""
        self.photo_upload_states[user_id] = post_name

    async def get_photo_upload_state(self, user_id):
        """Get current post name for photo upload"""
        return self.photo_upload_states.get(user_id)

    async def clear_photo_upload_state(self, user_id):
        """Clear photo upload state"""
        if user_id in self.photo_upload_states:
            del self.photo_upload_states[user_id]

    def update_config(self, new_config):
        """Update spam bot configuration"""
        # Don't modify main config, update channels.txt instead
        channels_file = "spam_bot/channels.txt"
        with open(channels_file, 'w', encoding='utf-8') as f:
            for channel in new_config.get('groups', []):
                f.write(str(channel) + '\n')


# Create global spam bot manager instance
spam_manager = SpamBotManager()


@dp.message(Command("spam_panel"))
async def cmd_spam_panel(message: Message):
    """Open spam bot management panel"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return

    status = "🟢 Активен" if spam_manager.is_running else "🔴 Остановлен"
    active_posts = len([f for f in os.listdir("spam_bot/texts") if f.endswith('.txt')]) if os.path.exists(
        "spam_bot/texts") else 0

    keyboard = [
        [InlineKeyboardButton(text="🟢 Запустить спам", callback_data="spam_start")],
        [InlineKeyboardButton(text="🔴 Остановить спам", callback_data="spam_stop")],
        [InlineKeyboardButton(text="📝 Управление постами", callback_data="spam_manage_posts")],
        [InlineKeyboardButton(text="📜 Логи", callback_data="spam_view_logs")],
        [InlineKeyboardButton(text="🗑️ Очистить логи", callback_data="spam_clear_logs")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="spam_stats")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]
    ]

    await message.answer(
        "<b>📢 Панель управления спам-ботом</b>\n\n"
        f"Статус: {status}\n"
        f"Активных постов: {active_posts}\n"
        f"Последние события: {len(spam_manager.logs)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "spam_panel")
async def cb_spam_panel(query: CallbackQuery):
    """Open spam bot management panel from callback"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    status = "🟢 Активен" if spam_manager.is_running else "🔴 Остановлен"
    active_posts = len([f for f in os.listdir("spam_bot/texts") if f.endswith('.txt')]) if os.path.exists(
        "spam_bot/texts") else 0

    keyboard = [
        [InlineKeyboardButton(text="🟢 Запустить спам", callback_data="spam_start")],
        [InlineKeyboardButton(text="🔴 Остановить спам", callback_data="spam_stop")],
        [InlineKeyboardButton(text="📝 Управление постами", callback_data="spam_manage_posts")],
        [InlineKeyboardButton(text="📜 Логи", callback_data="spam_view_logs")],
        [InlineKeyboardButton(text="🗑️ Очистить логи", callback_data="spam_clear_logs")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="spam_stats")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]
    ]

    await query.message.edit_text(
        "<b>📢 Панель управления спам-ботом</b>\n\n"
        f"Статус: {status}\n"
        f"Активных постов: {active_posts}\n"
        f"Последние события: {len(spam_manager.logs)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "spam_start")
async def cb_spam_start(query: CallbackQuery):
    """Start the spam bot"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    if spam_manager.is_running:
        await query.answer("Спам-бот уже запущен!", show_alert=True)
        return

    # Start spam bot in background
    spam_manager.is_running = True
    spam_manager.add_log("Spam bot started by admin")
    asyncio.create_task(run_spam_bot())

    await query.answer("✅ Спам-бот запущен!")
    await cb_spam_panel(query)


@dp.callback_query(F.data == "spam_stop")
async def cb_spam_stop(query: CallbackQuery):
    """Stop the spam bot"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    if not spam_manager.is_running:
        await query.answer("Спам-бот уже остановлен!", show_alert=True)
        return

    spam_manager.is_running = False
    spam_manager.add_log("Spam bot stopped by admin")

    await query.answer("✅ Спам-бот остановлен!")
    await cb_spam_panel(query)


@dp.callback_query(F.data == "spam_view_logs")
async def cb_spam_logs(query: CallbackQuery):
    """View spam bot logs"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    if not spam_manager.logs:
        logs_text = "Логи пусты"
    else:
        logs_text = "\n".join(spam_manager.get_logs(10))

    keyboard = [
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="spam_view_logs")],
        [InlineKeyboardButton(text="🗑️ Очистить логи", callback_data="spam_clear_logs")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_panel")]
    ]

    await query.message.edit_text(
        "<b>📜 Последние события</b>\n\n" + logs_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "spam_clear_logs")
async def cb_spam_clear_logs(query: CallbackQuery):
    """Clear spam bot logs"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    spam_manager.clear_logs()
    spam_manager.add_log("Logs cleared by admin")

    await query.answer("✅ Логи очищены!")
    await cb_spam_panel(query)


@dp.callback_query(F.data == "spam_stats")
async def cb_spam_stats(query: CallbackQuery):
    """View spam bot statistics"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    active_posts = len([f for f in os.listdir("spam_bot/texts") if f.endswith('.txt')]) if os.path.exists(
        "spam_bot/texts") else 0
    total_logs = len(spam_manager.logs)
    status = "🟢 Активен" if spam_manager.is_running else "🔴 Остановлен"

    # Read config to get channel count
    config_file = "spam_bot/config.json"
    channel_count = 0
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
            channel_count = len(config.get('groups', []))

    keyboard = [
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="spam_stats")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="spam_config")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_panel")]
    ]

    await query.message.edit_text(
        "<b>📊 Статистика спам-бота</b>\n\n"
        f"Статус: {status}\n"
        f"Активных постов: {active_posts}\n"
        f"Каналов для рассылки: {channel_count}\n"
        f"Всего событий в логах: {total_logs}\n"
        f"Последнее событие: {spam_manager.logs[-1] if spam_manager.logs else 'Нет событий'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "spam_config")
async def cb_spam_config(query: CallbackQuery):
    """View spam bot configuration"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    # Read current config
    config_file = "spam_bot/config.json"
    if os.path.exists(config_file):
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
    else:
        # Load default config
        config = {
            "groups": [],
            "post_interval_seconds": 120,
            "cycle_interval_seconds": 3600,
            "session_file": "session/userbot",
            "log_file": "logs/bot.log",
            "log_level": "INFO"
        }

    keyboard = [
        [InlineKeyboardButton(text="🕐 Изменить интервал постов", callback_data="spam_change_post_interval")],
        [InlineKeyboardButton(text="⏱️ Изменить интервал цикла", callback_data="spam_change_cycle_interval")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_stats")]
    ]

    await query.message.edit_text(
        "<b>⚙️ Настройки спам-бота</b>\n\n"
        f"<b>Текущие настройки:</b>\n"
        f"• Интервал между постами: {config.get('post_interval_seconds', 120)} секунд\n"
        f"• Интервал между циклами: {config.get('cycle_interval_seconds', 3600)} секунд\n"
        f"• Количество каналов: {len(config.get('groups', []))}\n"
        f"• Активных постов: {len([f for f in os.listdir('spam_bot/texts') if f.endswith('.txt')]) if os.path.exists('spam_bot/texts') else 0}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "spam_change_post_interval")
async def cb_spam_change_post_interval(query: CallbackQuery, state: FSMContext):
    """Change post interval setting"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    await query.message.edit_text(
        "<b>🕐 Изменить интервал между постами</b>\n\n"
        f"Введите новый интервал между постами в <b>секундах</b>.\n\n"
        f"Например, 120 для 2 минут:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_config")]
        ]),
        parse_mode="HTML"
    )

    await state.update_data(setting="post_interval")
    await state.set_state(FormState.waiting_for_new_sheet_name)


@dp.callback_query(F.data == "spam_change_cycle_interval")
async def cb_spam_change_cycle_interval(query: CallbackQuery, state: FSMContext):
    """Change cycle interval setting"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    await query.message.edit_text(
        "<b>⏱️ Изменить интервал между циклами</b>\n\n"
        f"Введите новый интервал между циклами в <b>секундах</b>.\n\n"
        f"Например, 3600 для 1 часа:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_config")]
        ]),
        parse_mode="HTML"
    )

    await state.update_data(setting="cycle_interval")
    await state.set_state(FormState.waiting_for_new_sheet_name)


@dp.message(FormState.waiting_for_new_sheet_name)
async def process_spam_config_update(message: Message, state: FSMContext):
    """Process spam bot configuration update"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        await state.clear()
        return

    data = await state.get_data()
    setting = data.get('setting')

    if setting:
        # Handle config update (intervals)
        try:
            new_value = int(message.text.strip())

            if new_value <= 0:
                await message.answer("❌ Значение должно быть положительным числом.")
                return

            # Read current config
            config_file = "spam_bot/config.json"
            if os.path.exists(config_file):
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
            else:
                # Load default config
                config = {
                    "groups": [],
                    "post_interval_seconds": 120,
                    "cycle_interval_seconds": 3600,
                    "session_file": "spam_bot/session/userbot",
                    "log_file": "spam_bot/logs/bot.log",
                    "log_level": "INFO"
                }

            # Update the specific setting
            if setting == "post_interval":
                config['post_interval_seconds'] = new_value
                spam_manager.add_log(f"Post interval changed to {new_value}s by admin")
                await message.answer(f"✅ Интервал между постами изменен на {new_value} секунд!")
            elif setting == "cycle_interval":
                config['cycle_interval_seconds'] = new_value
                spam_manager.add_log(f"Cycle interval changed to {new_value}s by admin")
                await message.answer(f"✅ Интервал между циклами изменен на {new_value} секунд!")

            # Write back to file
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            await state.clear()
        except ValueError:
            await message.answer("❌ Пожалуйста, введите корректное число.")
    else:
        # Handle adding channel (wasn't related to configuration)
        channel_input = message.text.strip()

        # Validate channel format
        if not channel_input:
            await message.answer("❌ Пожалуйста, введите канал.")
            return

        # Check if it's a username (starts with @) or numeric ID (starts with -)
        if not (channel_input.startswith('@') or (
                channel_input.lstrip('-').isdigit() and channel_input.count('-') <= 1)):
            await message.answer("❌ Неверный формат канала. Используйте @username или числовой ID (-1001234567890).")
            return

        # Read current config
        config_file = "spam_bot/config.json"
        if os.path.exists(config_file):
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
        else:
            # Load default config
            config = {
                "groups": [],
                "post_interval_seconds": 120,
                "cycle_interval_seconds": 3600,
                "session_file": "spam_bot/session/userbot",
                "log_file": "spam_bot/logs/bot.log",
                "log_level": "INFO"
            }

        # Convert channel_input to proper type (int for numeric, str for username)
        try:
            if channel_input.lstrip('-').isdigit():
                channel_to_add = int(channel_input)
            else:
                channel_to_add = channel_input
        except ValueError:
            await message.answer("❌ Неверный формат числового ID канала.")
            return

        # Add channel if it's not already in the list
        if channel_to_add not in config['groups']:
            config['groups'].append(channel_to_add)

            # Write back to file
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)

            spam_manager.add_log(f"Channel '{channel_to_add}' added by admin")
            await message.answer(f"✅ Канал '{channel_to_add}' добавлен!")
        else:
            await message.answer(f"⚠️ Канал '{channel_to_add}' уже существует в списке!")

        # Clear state
        await state.clear()


@dp.message(FormState.waiting_for_increase_amount)
async def process_spam_add_post_step2(message: Message, state: FSMContext):
    """Process new post text"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return

    data = await state.get_data()
    if 'new_post_name' in data:
        post_name = data['new_post_name']

        # Update post content
        post_file = f"spam_bot/texts/{post_name}.txt"
        os.makedirs(os.path.dirname(post_file), exist_ok=True)

        with open(post_file, 'w', encoding='utf-8') as f:
            f.write(message.text)

        spam_manager.add_log(f"Post '{post_name}' text updated by admin")

        await message.answer(
            f"✅ Пост '{post_name}' создан и обновлен!\n\n"
            f"Текст:\n{message.text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Редактировать", callback_data=f"spam_edit_post_{post_name}")],
                [InlineKeyboardButton(text="📦 Добавить фото", callback_data=f"spam_add_photo_{post_name}")],
                [InlineKeyboardButton(text="📰 Управление постами", callback_data="spam_manage_posts")]
            ])
        )

        await state.clear()
    else:
        await state.clear()


@dp.callback_query(F.data == "spam_add_post")
async def cb_spam_add_post(query: CallbackQuery, state: FSMContext):
    """Prompt to add a new post"""
    try:
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
            return
        
        await query.message.edit_text(
            "<b>📝 Добавить новый пост</b>\n\n"
            "Введите название нового поста (латиницей, без пробелов и специальных символов):\n\n"
            "Например: 'post1', 'announcement', 'news'",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_manage_posts")]
            ]),
            parse_mode="HTML"
        )
        
        await state.set_state(FormState.waiting_for_new_post_name)
    except Exception as e:
        logging.error(f"Error in cb_spam_add_post: {e}")
        await query.answer("❌ Произошла ошибка. Попробуйте позже.", show_alert=True)


@dp.callback_query(F.data == "spam_manage_posts")
async def cb_spam_manage_posts(query: CallbackQuery):
    """Manage spam posts"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    # List available posts
    texts_dir = "spam_bot/texts"
    if os.path.exists(texts_dir):
        posts = [f for f in os.listdir(texts_dir) if f.endswith('.txt')]
    else:
        posts = []

    keyboard = []

    # Add buttons for each existing post
    for post in posts:
        post_name = post[:-4]  # Remove .txt extension
        keyboard.append([InlineKeyboardButton(text=f"📝 {post_name}", callback_data=f"spam_edit_post_{post_name}")])

    # Add management buttons
    keyboard.append([InlineKeyboardButton(text="🆕 Добавить пост", callback_data="spam_add_post")])
    if posts:
        keyboard.append([InlineKeyboardButton(text="🗑️ Удалить пост", callback_data="spam_delete_post_list")])

    # Add channels management button
    keyboard.append([InlineKeyboardButton(text="📡 Управление каналами", callback_data="spam_manage_channels")])

    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_panel")])

    await query.message.edit_text(
        "<b>📝 Управление постами</b>\n\n"
        f"Доступные посты: {len(posts)}\n\n"
        f"Для редактирования нажмите на пост",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "spam_manage_channels")
async def cb_spam_manage_channels(query: CallbackQuery):
    """Manage spam channels"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    # Read current channels from channels.txt file
    channels_file = "spam_bot/channels.txt"
    channels = []
    if os.path.exists(channels_file):
        with open(channels_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:  # Skip empty lines
                    channels.append(line)

    keyboard = []

    # Show current channels
    for channel in channels:
        # Convert channel to string and encode special characters for callback data
        channel_str = str(channel)
        # Replace problematic characters for callback data
        safe_channel = channel_str.replace("-", "_minus_").replace("@", "_at_")
        keyboard.append(
            [InlineKeyboardButton(text=f"📡 {str(channel)}", callback_data=f"spam_channel_action_{safe_channel}")])

    # Add channel management buttons
    keyboard.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="spam_add_channel")])
    if channels:
        keyboard.append([InlineKeyboardButton(text="🗑️ Удалить канал", callback_data="spam_delete_channel_list")])

    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_manage_posts")])

    await query.message.edit_text(
        "<b>📡 Управление каналами</b>\n\n"
        f"Каналы для рассылки: {len(channels)}\n\n"
        f"Текущие каналы:\n" + ("\n".join([f"- {ch}" for ch in channels]) if channels else "- Нет каналов"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("spam_add_channel"))
async def cb_spam_add_channel(query: CallbackQuery, state: FSMContext):
    """Prompt to add a new channel"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    await query.message.edit_text(
        "<b>➕ Добавить канал</b>\n\n"
        f"Введите канал для добавления в формате:\n"
        f"- @channel_name (для публичного канала)\n"
        f"- -1001234567890 (для приватного канала/группы)\n\n"
        f"Пример: @my_public_channel или -1001234567890",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_manage_channels")]
        ]),
        parse_mode="HTML"
    )

    await state.set_state(FormState.waiting_for_new_sheet_name)


@dp.message(FormState.waiting_for_new_sheet_name)
async def process_spam_add_channel(message: Message, state: FSMContext):
    """Process adding a new channel or other inputs depending on current state"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        await state.clear()
        return

    # Get current state data to determine what we're adding
    data = await state.get_data()

    # If we're in config update state (interval changes), use that handler
    if 'setting' in data:
        await process_spam_config_update(message, state)  # This function handles config changes
        return

    # Otherwise, treat as adding a channel
    channel_input = message.text.strip()

    # Validate channel format
    if not channel_input:
        await message.answer("❌ Пожалуйста, введите канал.")
        return

    # Check if it's a username (starts with @) or numeric ID (starts with -)
    if not (channel_input.startswith('@') or (channel_input.lstrip('-').isdigit() and channel_input.count('-') <= 1)):
        await message.answer("❌ Неверный формат канала. Используйте @username или числовой ID (-1001234567890).")
        return

    # Read current channels from file
    channels_file = "spam_bot/channels.txt"
    existing_channels = []
    if os.path.exists(channels_file):
        with open(channels_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:  # Skip empty lines
                    existing_channels.append(line)

    # Convert channel_input to proper type (int for numeric, str for username)
    try:
        if channel_input.lstrip('-').isdigit():
            channel_to_add = int(channel_input)
        else:
            channel_to_add = channel_input
    except ValueError:
        await message.answer("❌ Неверный формат числового ID канала.")
        return

    # Check if channel already exists
    if str(channel_to_add) in existing_channels:
        await message.answer(f"⚠️ Канал '{channel_to_add}' уже существует в списке!")
        await state.clear()
        return

    # Add channel to file
    with open(channels_file, 'a', encoding='utf-8') as f:
        f.write(str(channel_to_add) + '\n')

    spam_manager.add_log(f"Channel '{channel_to_add}' added by admin")
    await message.answer(f"✅ Канал '{channel_to_add}' добавлен!")

    # Clear state
    await state.clear()


@dp.callback_query(F.data == "spam_delete_channel_list")
async def cb_spam_delete_channel_list(query: CallbackQuery):
    """Show list of channels to delete"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    # Read current channels from channels.txt file
    channels_file = "spam_bot/channels.txt"
    channels = []
    if os.path.exists(channels_file):
        with open(channels_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:  # Skip empty lines
                    channels.append(line)

    if not channels:
        await query.answer("❌ Нет каналов для удаления!", show_alert=True)
        return

    keyboard = []

    # Show current channels with delete option
    for channel in channels:
        # Encode channel name for callback data
        channel_str = str(channel)
        safe_channel = channel_str.replace("-", "_minus_").replace("@", "_at_")
        keyboard.append([InlineKeyboardButton(text=f"🗑️ Удалить {str(channel)}",
                                              callback_data=f"spam_confirm_delete_channel_{safe_channel}")])

    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_manage_channels")])

    await query.message.edit_text(
        "<b>🗑️ Выберите канал для удаления</b>\n\n"
        f"Выберите канал, который хотите удалить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("spam_confirm_delete_channel_"))
async def cb_spam_confirm_delete_channel(query: CallbackQuery):
    """Confirm channel deletion"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    # Decode the safe channel name back to original
    encoded_channel = query.data.replace("spam_confirm_delete_channel_", "")
    original_channel = encoded_channel.replace("_minus_", "-").replace("_at_", "@")

    # Create confirmation buttons
    keyboard = [
        [InlineKeyboardButton(text="❌ Да, удалить", callback_data=f"spam_do_delete_channel_{encoded_channel}")],
        [InlineKeyboardButton(text="✅ Нет, отмена", callback_data="spam_manage_channels")]
    ]

    await query.message.edit_text(
        f" ⚠️ <b>Вы уверены, что хотите удалить канал '{original_channel}'?</b>\n\n"
        f"Это действие нельзя будет отменить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("spam_do_delete_channel_"))
async def cb_spam_do_delete_channel(query: CallbackQuery):
    """Actually delete the channel"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    # Decode the safe channel name back to original
    encoded_channel = query.data.replace("spam_do_delete_channel_", "")
    original_channel = encoded_channel.replace("_minus_", "-").replace("_at_", "@")

    # Read current config
    channels_file = "spam_bot/channels.txt"
    if os.path.exists(channels_file):
        # Read all channels
        with open(channels_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        # Find and remove the channel
        updated_lines = []
        channel_found = False
        for line in lines:
            stripped_line = line.strip()
            if stripped_line == str(original_channel):
                channel_found = True
            else:
                updated_lines.append(line)

        if channel_found:
            # Write back updated list
            with open(channels_file, 'w', encoding='utf-8') as f:
                f.writelines(updated_lines)

            spam_manager.add_log(f"Channel '{original_channel}' deleted by admin")
            await query.answer(f"✅ Канал '{original_channel}' удален!")
        else:
            await query.answer("❌ Канал уже удален или не существует!", show_alert=True)
    else:
        await query.answer("❌ Не найден файл каналов!", show_alert=True)

    # Go back to channel management
    await cb_spam_manage_channels(query)


@dp.callback_query(F.data.startswith("spam_channel_action_"))
async def cb_spam_channel_action(query: CallbackQuery):
    """Action on a specific channel"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    # Decode the safe channel name back to original
    encoded_channel = query.data.replace("spam_channel_action_", "")
    # Reverse the encoding to get the original channel name
    original_channel = encoded_channel.replace("_minus_", "-").replace("_at_", "@")

    keyboard = [
        [InlineKeyboardButton(text="🗑️ Удалить канал", callback_data=f"spam_confirm_delete_channel_{encoded_channel}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_manage_channels")]
    ]

    await query.message.edit_text(
        "<b>📡 Действия с каналом</b>\n\n"
        f"Выбранный канал: {original_channel}\n\n"
        f"Выберите действие:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.message(FormState.waiting_for_new_post_name)
async def process_spam_add_post_name_step1(message: Message, state: FSMContext):
    """Process new post name step 1"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return

    post_name = message.text.strip()

    # Validate post name (only alphanumeric and underscore)
    if not post_name or not post_name.replace('_', '').isalnum():
        await message.answer("❌ Неверное название поста. Используйте только буквы, цифры и символ подчеркивания.")
        return

    # Create post file
    post_file = f"spam_bot/texts/{post_name}.txt"
    os.makedirs(os.path.dirname(post_file), exist_ok=True)

    with open(post_file, 'w', encoding='utf-8') as f:
        f.write("")  # Empty post content initially

    spam_manager.add_log(f"Post '{post_name}' created by admin")

    await message.answer(
        f"✅ Пост '{post_name}' создан!\n\n"
        "Теперь отправьте текст для этого поста:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_manage_posts")]
        ])
    )

    # Store post name for next step
    await state.update_data(new_post_name=post_name)
    # Update state to wait for post text
    await state.set_state(FormState.waiting_for_new_post_text)


@dp.callback_query(F.data.startswith("spam_edit_post_"))
async def cb_spam_edit_post(query: CallbackQuery, state: FSMContext):
    """Edit a specific spam post"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    post_name = query.data.split("_")[3]  # Get the post name from callback data

    # Read current post content
    post_file = f"spam_bot/texts/{post_name}.txt"
    content = ""
    if os.path.exists(post_file):
        with open(post_file, 'r', encoding='utf-8') as f:
            content = f.read()

    await query.message.edit_text(
        f"<b>📝 Редактирование поста {post_name}</b>\n\n"
        f"Текущий текст поста '{post_name}':\n{content if content else 'Пусто'}\n\n"
        f"Отправьте новый текст для поста:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_manage_posts")]
        ]),
        parse_mode="HTML"
    )

    await state.update_data(editing_post=post_name)
    await state.set_state(FormState.waiting_for_edit_post_text)


@dp.message(FormState.waiting_for_new_post_text)
async def process_spam_add_post_text_step2(message: Message, state: FSMContext):
    """Process new post text step 2"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return

    data = await state.get_data()
    post_name = data.get('new_post_name')

    if not post_name:
        await message.answer("❌ Ошибка: пост не найден. Попробуйте создать пост заново.")
        await state.clear()
        return

    # Update post content
    post_file = f"spam_bot/texts/{post_name}.txt"
    os.makedirs(os.path.dirname(post_file), exist_ok=True)

    with open(post_file, 'w', encoding='utf-8') as f:
        f.write(message.text)

    spam_manager.add_log(f"Post '{post_name}' text updated by admin")

    await message.answer(
        f"✅ Пост '{post_name}' создан и текст сохранен!\n\n"
        f"Текст:\n{message.text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Редактировать пост", callback_data=f"spam_edit_post_{post_name}")],
            [InlineKeyboardButton(text="📦 Добавить фото", callback_data=f"spam_add_photo_{post_name}")],
            [InlineKeyboardButton(text="📰 Управление постами", callback_data="spam_manage_posts")]
        ])
    )

    await state.clear()


@dp.message(F.photo, FormState.waiting_for_product_data)
async def process_spam_post_image(message: Message, state: FSMContext):
    """Process image attachment for post (editing existing post)"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return

    data = await state.get_data()
    post_name = data.get('editing_post')

    if post_name:
        # Download and save the image
        # Get the highest quality photo
        photo = message.photo[-1]  # Last element is the highest quality

        # Create file with the same name as post
        file_extension = '.jpg'  # Telegram photos are usually jpg
        photo_path = f"spam_bot/photos/{post_name}{file_extension}"
        os.makedirs(os.path.dirname(photo_path), exist_ok=True)

        # Download the photo
        await message.bot.download(photo.file_id, photo_path)

        await message.answer(
            f"✅ Фото для поста '{post_name}' успешно добавлено!\n\n"
            f"Путь к фото: {photo_path}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Редактировать текст", callback_data=f"spam_edit_post_{post_name}")],
                [InlineKeyboardButton(text="📰 Управление постами", callback_data="spam_manage_posts")]
            ])
        )

        await state.clear()
    else:
        await message.answer("❌ Нет активного поста для добавления фото. Сначала создайте или выберите пост.")


@dp.message(F.photo, FormState.waiting_for_new_post_text)
async def process_spam_new_post_image(message: Message, state: FSMContext):
    """Process image attachment for new post"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return

    data = await state.get_data()
    post_name = data.get('new_post_name')

    if post_name:
        # Download and save the image
        # Get the highest quality photo
        photo = message.photo[-1]  # Last element is the highest quality

        # Create file with the same name as post
        file_extension = '.jpg'  # Telegram photos are usually jpg
        photo_path = f"spam_bot/photos/{post_name}{file_extension}"
        os.makedirs(os.path.dirname(photo_path), exist_ok=True)

        # Download the photo
        await message.bot.download(photo.file_id, photo_path)

        await message.answer(
            f"✅ Фото для нового поста '{post_name}' успешно добавлено!\n\n"
            f"Путь к фото: {photo_path}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Редактировать текст", callback_data=f"spam_edit_post_{post_name}")],
                [InlineKeyboardButton(text="📰 Управление постами", callback_data="spam_manage_posts")]
            ])
        )

        await state.clear()
    else:
        await message.answer("❌ Ошибка: нет активного поста для добавления фото.")


@dp.message(F.document, FormState.waiting_for_product_data)
async def process_spam_post_document(message: Message, state: FSMContext):
    """Process document image attachment for post (editing existing post)"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return
    
    # Check if document is an image
    doc = message.document
    if doc.mime_type and doc.mime_type.startswith('image/'):
        data = await state.get_data()
        post_name = data.get('editing_post')
        
        if post_name:
            # Extract file extension
            file_extension = os.path.splitext(doc.file_name)[1].lower()
            if file_extension in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
                photo_path = f"spam_bot/photos/{post_name}{file_extension}"
                os.makedirs(os.path.dirname(photo_path), exist_ok=True)
                
                # Download the photo
                await message.bot.download(doc.file_id, photo_path)
                
                # Clear the state
                if message.from_user.id in spam_manager.photo_upload_states:
                    del spam_manager.photo_upload_states[message.from_user.id]
        
        await message.answer(
            f"✅ Фото для поста '{post_name}' успешно загружено!\n\n"
            f"Фото сохранено как: {photo_path}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Редактировать пост", callback_data=f"spam_edit_post_{post_name}")],
                [InlineKeyboardButton(text="📦 Заменить фото", callback_data=f"spam_add_photo_{post_name}")],
                [InlineKeyboardButton(text="📰 Управление постами", callback_data="spam_manage_posts")]
            ])
        )
    else:
        # If not in photo upload state, handle as usual (could be a different handler)
        pass


@dp.callback_query(F.data == "spam_delete_post_list")
async def cb_spam_delete_post_list(query: CallbackQuery):
    """Show list of posts to delete"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    # List available posts
    texts_dir = "spam_bot/texts"
    if os.path.exists(texts_dir):
        posts = [f for f in os.listdir(texts_dir) if f.endswith('.txt')]
    else:
        posts = []

    if not posts:
        await query.answer("❌ Нет постов для удаления!", show_alert=True)
        return

    keyboard = []

    # Show current posts with delete option
    for post in posts:
        post_name = post[:-4]  # Remove .txt extension
        keyboard.append([InlineKeyboardButton(text=f"🗑️ Удалить {post_name}",
                                              callback_data=f"spam_confirm_delete_post_{post_name}")])

    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_manage_posts")])

    await query.message.edit_text(
        "<b>🗑️ Выберите пост для удаления</b>\n\n"
        "Выберите пост, который хотите удалить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("spam_confirm_delete_post_"))
async def cb_spam_confirm_delete_post(query: CallbackQuery):
    """Confirm post deletion"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    post_name = query.data.replace("spam_confirm_delete_post_", "")

    # Create confirmation buttons
    keyboard = [
        [InlineKeyboardButton(text="❌ Да, удалить", callback_data=f"spam_do_delete_post_{post_name}")],
        [InlineKeyboardButton(text="✅ Нет, отмена", callback_data="spam_manage_posts")]
    ]

    await query.message.edit_text(
        f" ⚠️ <b>Вы уверены, что хотите удалить пост '{post_name}'?</b>\n\n"
        f"Это действие нельзя будет отменить, и файл поста будет удален.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("spam_do_delete_post_"))
async def cb_spam_do_delete_post(query: CallbackQuery):
    """Actually delete the post"""
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
        return

    post_name = query.data.replace("spam_do_delete_post_", "")
    post_file = f"spam_bot/texts/{post_name}.txt"

    if os.path.exists(post_file):
        os.remove(post_file)
        spam_manager.add_log(f"Post '{post_name}' deleted by admin")
        await query.answer(f"✅ Пост '{post_name}' удален!")
    else:
        await query.answer("❌ Пост уже удален или не существует!", show_alert=True)

    # Go back to post management
    await cb_spam_manage_posts(query)


async def run_spam_bot():
    """Run the spam bot separately"""
    # Change to the spam bot directory
    original_cwd = os.getcwd()
    try:
        os.chdir("spam_bot")

        # Import and run the spam bot with custom logging
        from main import main as spam_main
        from main import set_spam_manager

        # Set the spam manager reference so the spam bot can log to main bot
        set_spam_manager(spam_manager)

        await spam_main()
    except ImportError:
        spam_manager.add_log("ERROR: Could not import spam bot")
        logging.error("Could not import spam bot")
    except Exception as e:
        spam_manager.add_log(f"ERROR: {str(e)}")
        logging.error(f"Error running spam bot: {e}")
    finally:
        spam_manager.is_running = False
        os.chdir(original_cwd)


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
                        media = InputMediaPhoto(media=product_info['photo_url'], caption=text_with_time,
                                                parse_mode="HTML")
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
                [InlineKeyboardButton(text="➕ +1",
                                      callback_data=f"confirm_increase|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|1")],
                [InlineKeyboardButton(text="➕ +5",
                                      callback_data=f"confirm_increase|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|5")],
                [InlineKeyboardButton(text="➕ +10",
                                      callback_data=f"confirm_increase|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|10")],
                [InlineKeyboardButton(text="🔢 Ввести свое",
                                      callback_data=f"input_increase|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}")],
                [InlineKeyboardButton(text="❌ Отмена",
                                      callback_data=f"cancel_change|{sheet_name_encoded}|{row_num}|{col_index}")]
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
                [InlineKeyboardButton(text="➖ -1",
                                      callback_data=f"confirm_decrease|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|1")],
                [InlineKeyboardButton(text="➖ -5",
                                      callback_data=f"confirm_decrease|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|5")],
                [InlineKeyboardButton(text="➖ -10",
                                      callback_data=f"confirm_decrease|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}|10")],
                [InlineKeyboardButton(text="🔢 Ввести свое",
                                      callback_data=f"input_decrease|{sheet_name_encoded}|{row_num}|{col_index}|{current_qty}")],
                [InlineKeyboardButton(text="❌ Отмена",
                                      callback_data=f"cancel_change|{sheet_name_encoded}|{row_num}|{col_index}")]
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


@dp.message(FormState.waiting_for_edit_post_text)
async def process_spam_edit_post_text(message: Message, state: FSMContext):
    """Process spam post text when editing existing post"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет прав для использования этой команды.")
        return

    data = await state.get_data()
    post_name = data.get('editing_post')

    if post_name:
        # Update post content
        post_file = f"spam_bot/texts/{post_name}.txt"
        os.makedirs(os.path.dirname(post_file), exist_ok=True)

        with open(post_file, 'w', encoding='utf-8') as f:
            f.write(message.text)

        spam_manager.add_log(f"Post '{post_name}' updated by admin")

        await message.answer(
            f"✅ Пост '{post_name}' обновлен!\n\n"
            f"Новый текст:\n{message.text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Редактировать еще", callback_data=f"spam_edit_post_{post_name}")],
                [InlineKeyboardButton(text="📦 Добавить фото", callback_data=f"spam_add_photo_{post_name}")],
                [InlineKeyboardButton(text="📰 Управление постами", callback_data="spam_manage_posts")]
            ])
        )

        await state.clear()
    else:
        await message.answer("❌ Ошибка состояния. Попробуйте начать заново.")
        await state.clear()


# Add handler for photo uploads in any state
@dp.message(F.photo)
async def handle_photo_upload(message: Message):
    """Handle photo uploads for posts"""
    try:
        if message.from_user.id not in ADMIN_IDS:
            return  # Don't process if not admin
        
        # Check if user is in photo upload state
        post_name = spam_manager.photo_upload_states.get(message.from_user.id)
        
        if post_name:
            # Get the highest quality photo
            photo = message.photo[-1]  # Last element is the highest quality
            
            # Create file with the same name as post
            file_extension = '.jpg'  # Telegram photos are usually jpg
            photo_path = f"spam_bot/photos/{post_name}{file_extension}"
            os.makedirs(os.path.dirname(photo_path), exist_ok=True)
            
            # Download the photo
            await message.bot.download(photo.file_id, photo_path)
            
            # Clear the state
            if message.from_user.id in spam_manager.photo_upload_states:
                del spam_manager.photo_upload_states[message.from_user.id]
            
            await message.answer(
                f"✅ Фото для поста '{post_name}' успешно загружено!\n\n"
                f"Фото сохранено как: {photo_path}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📝 Редактировать пост", callback_data=f"spam_edit_post_{post_name}")],
                    [InlineKeyboardButton(text="📦 Заменить фото", callback_data=f"spam_add_photo_{post_name}")],
                    [InlineKeyboardButton(text="📰 Управление постами", callback_data="spam_manage_posts")]
                ])
            )
        else:
            # If not in photo upload state, handle as a general photo
            await message.answer("❌ Нет активного поста для добавления фото. Сначала создайте или выберите пост.")
    except Exception as e:
        logging.error(f"Error in handle_photo_upload: {e}")
        await message.answer("❌ Произошла ошибка при загрузке фото. Попробуйте позже.")


@dp.callback_query(F.data.startswith("spam_add_photo_"))
async def cb_spam_add_photo(query: CallbackQuery):
    """Prompt to add photo for post"""
    try:
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("❌ У вас нет прав для использования этой команды.", show_alert=True)
            return
        
        # Extract post name from callback data: "spam_add_photo_{post_name}"
        post_name = query.data[len("spam_add_photo_"):].strip()
        
        await query.message.edit_text(
            f"<b>📦 Добавить фото к посту '{post_name}'</b>\n\n"
            f"Теперь вы можете отправить фото прямо в этот чат, и оно будет автоматически сохранено как '{post_name}.jpg' в папке 'spam_bot/photos/'.\n\n"
            f"Поддерживаемые форматы: .jpg, .jpeg, .png\n\n"
            f"Отправьте фото, которое хотите использовать для поста '{post_name}':",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Редактировать пост", callback_data=f"spam_edit_post_{post_name}")],
                [InlineKeyboardButton(text="📰 Управление постами", callback_data="spam_manage_posts")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="spam_manage_posts")]
            ]),
            parse_mode="HTML"
        )
        
        # Set state to wait for photo upload
        spam_manager.photo_upload_states[query.from_user.id] = post_name
    except Exception as e:
        logging.error(f"Error in cb_spam_add_photo: {e}")
        await query.answer("❌ Произошла ошибка. Попробуйте позже.", show_alert=True)


@dp.message(F.document)
async def handle_document_upload(message: Message):
    """Handle document uploads (for images)"""
    try:
        if message.from_user.id not in ADMIN_IDS:
            return  # Don't process if not admin
        
        # Check if it's an image document
        doc = message.document
        if doc.mime_type and doc.mime_type.startswith('image/'):
            # Check if user is in photo upload state
            post_name = spam_manager.photo_upload_states.get(message.from_user.id)
            
            if post_name:
                # Extract file extension from original filename
                file_extension = os.path.splitext(doc.file_name)[1].lower()
                if file_extension in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
                    photo_path = f"spam_bot/photos/{post_name}{file_extension}"
                    os.makedirs(os.path.dirname(photo_path), exist_ok=True)
                    
                    # Download the photo
                    await message.bot.download(doc.file_id, photo_path)
                    
                    # Clear the state
                    if message.from_user.id in spam_manager.photo_upload_states:
                        del spam_manager.photo_upload_states[message.from_user.id]
                    
                    await message.answer(
                        f"✅ Фото для поста '{post_name}' успешно загружено!\n\n"
                        f"Фото сохранено как: {photo_path}",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="📝 Редактировать пост", callback_data=f"spam_edit_post_{post_name}")],
                            [InlineKeyboardButton(text="📦 Заменить фото", callback_data=f"spam_add_photo_{post_name}")],
                            [InlineKeyboardButton(text="📰 Управление постами", callback_data="spam_manage_posts")]
                        ])
                    )
                else:
                    await message.answer("❌ Неподдерживаемый формат изображения. Используйте JPG, PNG, GIF, BMP или WEBP.")
            else:
                # If not in photo upload state, handle as a general document
                await message.answer("❌ Нет активного поста для добавления фото. Сначала создайте или выберите пост.")
        else:
            # This isn't an image document, pass to other handlers
            pass
    except Exception as e:
        logging.error(f"Error in handle_document_upload: {e}")
        await message.answer("❌ Произошла ошибка при загрузке документа. Попробуйте позже.")


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
