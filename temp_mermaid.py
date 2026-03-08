import base64
import json

code = """flowchart TD
    classDef module fill:#e1f5fe,stroke:#03a9f4,stroke-width:2px;
    classDef action fill:#fff,stroke:#b3e5fc,stroke-width:1px;

    subgraph Dashboard [Dashboard Module]
        D_Metrics(Revenue & Sales Charts)
        D_Alerts(Low Stock & PO Alerts)
    end
    class Dashboard module

    subgraph Assistant [Assistant Module]
        A_Chat(Ollama Chat Interface)
        A_Analytics(Natural Language Analytics)
    end
    class Assistant module

    subgraph Inventory [Inventory Module]
        I_List(Product List)
        I_History(Stock Movement History)
        
        I_Buttons[Create / Stock In / Stock Out / Adjust]
    end
    class Inventory module

    subgraph Sales [Sales Module]
        S_New(New Sale)
        S_History(Sales History)
        S_Details(Sale Details)
    end
    class Sales module

    subgraph Suppliers [Suppliers Module]
        Sup_Dir(Suppliers List)
        Sup_PO(Purchase Orders)
        Sup_Reorder(Reorder Alerts)
    end
    class Suppliers module

    S_New -- "1. Fetches Prices/Details" --> I_List
    S_New -- "2. Deducts Stock\n(Logs Sale Movement)" --> I_History
    S_New -- "3. Saves Transaction" --> S_History
    S_History -- "View Itemized" --> S_Details

    Sup_Reorder -- "Detects Low Stock" --> I_List
    Sup_Reorder -- "Generates" --> Sup_PO
    Sup_PO -- "Marks Received / Adds Stock\n(Logs Purchase Movement)" --> I_History

    I_Buttons -- "Applies Manual Changes\n(Logs In/Out/Adjust)" --> I_History
    I_Buttons -- "Adds New Item" --> I_List

    S_History -. "Feeds Revenue Data" .-> D_Metrics
    I_List -. "Feeds Stock Levels" .-> D_Alerts
    Sup_PO -. "Feeds Order Status" .-> D_Alerts

    I_History -. "Reads Data" .-> A_Analytics
    S_History -. "Reads Data" .-> A_Analytics
    Sup_PO -. "Reads Data" .-> A_Analytics
    I_List -. "Reads Data" .-> A_Analytics
    A_Analytics -- "Generates Insights\n& Answers" --> A_Chat
"""

payload = {
    "code": code,
    "mermaid": '{"theme":"default"}'
}
json_str = json.dumps(payload)
encoded = base64.urlsafe_b64encode(json_str.encode('utf-8')).decode('utf-8')
print("https://mermaid.ink/img/" + encoded)
