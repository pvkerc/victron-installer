# Victron installer

Openbare distributie van de Home Assistant-app **Victron updates**.
Deze repository bevat uitsluitend de installatie-app en haar offline proeven.
De Victron-regeling en onderzoeksgegevens staan in een afzonderlijke privérepository.

## Installeren via de cloud

1. Open Home Assistant via je gebruikelijke externe webadres.
2. Ga naar Instellingen → Apps → Appwinkel → menu → Repositories.
3. Voeg `https://github.com/pvkerc/victron-installer` toe.
4. Installeer **Victron updates**.
5. Vul bij de appconfiguratie een GitHub fine-grained token in, beperkt tot
   `pvkerc/victron-updates`, met **Contents: Read-only** en **Actions: Read-only**.
   Zet een vervaldatum. Deel de token niet in een chat of repository.
6. Start de app en open de webinterface.

Je hoeft geen gedeelde map te openen, terminal te gebruiken of bestanden te downloaden.
Na een expliciete versiegoedkeuring haalt de Green het pakket zelf op.

## Gebruik

De app toont alleen pakketten waarvan de GitHub-controles voor dezelfde broncommit geslaagd zijn.
Stop voor de installatie de Victron-regeling en P1 gecontroleerd, met laadstop aan.
De app controleert dat, maakt een Home Assistant-back-up, stopt Core, vervangt de component
en start Core. De geladen versie wordt gecontroleerd. Laadstop blijft aan.

Een GitHub-commit of een herstart start geen installatie.
Goedkeuring is gebonden aan precies de getoonde versie en vervalt na 15 minuten.
De vorige component en het kenmerk van de Home Assistant-back-up blijven beschikbaar.
Bij uitval van Core gebruik je de Home Assistant-back-up; de app omzeilt geen ontbrekende veiligheidsstatus.

## Rechten en controle

De app gebruikt HA-ingress, opent geen hostpoort en accepteert alleen de Supervisor-proxy.
Ze heeft schrijfbare toegang tot de HA-configuratiemap, Core-status en Supervisor-rol
**manager** voor de combinatie van back-up en Core-stop/start.
Ze roept geen batterij-, P1- of andere hardwarebesturingsacties aan.

De app is offline getest en voor aarch64 gebouwd; de eerste echte installatie moet nog op de Green worden geaccepteerd.
