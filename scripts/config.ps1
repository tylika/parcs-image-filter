# Налаштування кластера в Google Cloud. Тут можна змінювати значення.
$ProjectId    = 'project-a53af79b-1c08-4c03-95e'
$Prefix       = 'yulparcs'            # усі мої ресурси мають ім'я yulparcs-*  (Максим використовує інший префікс)
$Region       = 'europe-west1'
$Zones        = @('europe-west1-b', 'europe-west1-c', 'europe-west1-d')   # пробуємо по черзі
$MachineType  = 'n1-standard-1'       # 1 vCPU, 3.75 ГБ - як у одногрупника
$WorkersWanted = 4
$ParcsImage   = 'hummer12007/parcs-node'
$Network      = 'default'
