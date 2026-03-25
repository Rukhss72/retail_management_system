# RetailFlow User Guide

## 1. What This System Does
RetailFlow helps you manage daily retail operations in one place:

- check stock levels
- create and review sales
- manage suppliers
- raise reorder requests
- create and receive purchase orders
- monitor dashboard trends and forecast risk
- ask the Retail Assistant for quick insights

## 2. Before You Start
- Open the system and go to the login page.
- Select your role: `Admin` or `Staff`.
- Enter your username and password.
- Enter the 6-digit verification code sent to your email.
- If you choose `Remember me`, that device can skip verification for 30 days.

If you forget your password, use `Forgot password?` on the login page.

## 3. Roles
### Admin
Admins can use the full system, including:

- all inventory actions
- supplier management
- purchase orders
- dashboard
- assistant
- approve or reject reorder requests

### Staff
Staff can use daily operations, including:

- Home
- Inventory
- Sales
- create reorder drafts
- submit reorder requests
- edit their own draft or submitted requests

Staff cannot manage supplier master data or purchase orders. Stock adjustment is also admin only.

## 4. Main Navigation
- `Home`: quick shortcuts, alerts, and recent activity
- `Inventory`: products, stock in, stock out, adjustments, movement history
- `Sales`: new sale, sales history, sale details, receipt print
- `Suppliers`: suppliers, reorder drafts, reorder requests, purchase orders
- `Dashboard`: admin-only sales, stock health, forecast, and best-seller view
- `Assistant`: admin-only AI chat for sales, stock, and forecast questions

## 5. Home
Use Home for a quick start.

- See low-stock, out-of-stock, and pending reorder counts.
- Open shortcuts for common tasks like `Create Sale`, `Stock In`, `Stock Out`, `Add Product`, `Add Supplier`, and `Create PO`.
- Review recent stock and sales activity.

## 6. Inventory
Use Inventory to manage products and stock movement.

### Products
- Filter by category, section, supplier, search, or stock status.
- Review SKU, product name, category, price, stock, and status.

### Create Product
- Add a new product with category, section, selling price, reorder level, and opening quantity.
- A primary supplier is required.
- Opening stock is added immediately when the product is saved.
- If the same active product already exists, stock may be added to that product instead of creating a duplicate.

### Stock In
Use `Stock In` when new stock arrives.

- select the product
- enter quantity
- add reference or comments if needed

### Stock Out
Use `Stock Out` for non-sale removals such as damage, transfer, or shrinkage.

- select the product
- enter quantity
- enter a reason

You cannot remove more stock than is available.

### Adjust
Admin only.

Use `Adjust` to correct stock, section, or selling price.

- set stock to the correct quantity
- enter a reason
- save the change

### History
Use the history tab to review stock movements such as:

- stock in
- stock out
- sales
- adjustments
- purchase-order receipts

## 7. Sales
Use Sales to create and review customer transactions.

### New Sale
- Add optional customer name and phone.
- Filter products by search, section, or category.
- Add items to the cart.
- Change quantity, price, and discount percent if needed.
- Select a payment method: `Cash`, `Card`, `Mobile Payment`, `Juice`, or `Other`.
- Click `Save Sale`.

Rules:

- out-of-stock items cannot be added
- the cart warns if quantity exceeds available stock
- a sale updates stock automatically when saved

### Sales History
- Review previous receipts
- open any sale to view full details

### Sale Details
- see item lines, cashier, payment method, customer details, totals
- print the receipt when needed

## 8. Suppliers And Reordering
The Suppliers area supports stock replenishment.

### Suppliers Tab
Admin only.

Use it to:

- add suppliers
- edit supplier details
- assign supplier categories
- set lead time
- activate or deactivate suppliers

### Create Reorder Draft
Use this tab to prepare restocking requests from suggested items.

- select products that need restocking
- enter required quantities
- create draft requests

One reorder request is created per supplier.

### Reorder Requests
Use this tab to manage draft and submitted requests.

Possible statuses:

- `Draft`
- `Submitted`
- `Approved`
- `Rejected`
- `Cancelled`

Staff can:

- edit their own draft or submitted requests
- save as draft
- submit to admin

Admins can:

- approve submitted requests
- reject submitted requests

### Purchase Orders
Admin only.

Use this tab to create and manage POs.

Possible statuses:

- `Pending`
- `Received`
- `Cancelled`

Key rules:

- only products linked to the selected supplier can be added
- received or cancelled POs cannot be edited
- only draft POs can be deleted
- when a PO is marked `Received`, stock is added automatically

## 9. Dashboard
Admin only.

The Dashboard is the management view for performance and risk.

Use it to monitor:

- sales today, this week, and this month
- forecast demand for the next 7 and 30 days
- inventory health
- sales trends
- best sellers
- category performance
- stock attention items
- forecasted demand around festivals

If no forecast data is loaded, forecast sections stay empty.

## 10. Retail Assistant
Admin access is required for the assistant page.

Use it to ask business questions such as:

- total sales today
- low-stock items
- top sellers this month
- next-week forecast
- what to restock

Features:

- persistent chat history
- separate chat threads
- quick question chips
- answers may include tables or charts

## 11. Trusted Devices
If you used `Remember me`, you can manage saved devices from `Trusted Devices`.

- review active devices
- revoke a device if it should no longer skip verification

## 12. Good Operating Practice
- Create products before recording stock or sales.
- Keep supplier links up to date so reorder requests and POs work correctly.
- Use `Stock Out` only for non-sale removals.
- Use `Adjust` only to correct data.
- Mark purchase orders as `Received` only when stock has physically arrived.

## 13. Common Issues
- `Role mismatch`: the selected role does not match your account.
- `Invalid code` or `Code expired`: request a new verification code.
- `Insufficient stock`: reduce the quantity before saving.
- `Product is not connected to the chosen supplier`: fix the supplier-product link first.
- No forecast shown on the dashboard: forecast data has not been loaded yet.
