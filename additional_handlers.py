# Additional handlers for increase/decrease callbacks
from aiogram import F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
import logging

from bot import dp, get_product_info, build_product_card, InputMediaPhoto

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