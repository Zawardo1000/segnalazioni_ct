-- Stile di osm2pgsql (output flex) per lo stradario: strade e confini amministrativi, con tutti i tag in jsonb.
-- Le tabelle vanno nello schema indicato da SCHEMA_OSM (importa_osm.py importa in osm_import e poi lo rinomina).
-- Le regole di competenza NON stanno qui ma in abbinamento.py: qui si copia OSM com'e'.

local schema = os.getenv('SCHEMA_OSM') or 'osm_import'

-- stessa lista di ESCLUSE in abbinamento.py
local escluse = {
    proposed = true, construction = true, abandoned = true, disused = true, razed = true, platform = true,
    bus_stop = true, elevator = true, raceway = true, services = true, rest_area = true, emergency_bay = true,
    escape = true, busway = true, via_ferrata = true, corridor = true,
}

local vie = osm2pgsql.define_way_table('vie', {
    { column = 'highway', type = 'text', not_null = true },
    { column = 'tags', type = 'jsonb' },
    { column = 'geom', type = 'linestring', projection = 4326, not_null = true },
}, { schema = schema })

-- Cartelli di inizio/fine centro abitato (traffic_sign=city_limit) e localita' (place=*, spesso con la
-- popolazione): servono a capire dove il Comune ha delimitato i centri abitati (art. 4 CdS), che e' cio' che
-- decide le traverse interne. Istat invece fonde i centri contigui (Acireale = un solo centro da 48 mila).
local punti = osm2pgsql.define_node_table('punti', {
    { column = 'tipo', type = 'text', not_null = true },
    { column = 'nome', type = 'text' },
    { column = 'tags', type = 'jsonb' },
    { column = 'geom', type = 'point', projection = 4326, not_null = true },
}, { schema = schema })

local localita = {
    city = true, town = true, village = true, hamlet = true, suburb = true, quarter = true, neighbourhood = true,
}

-- Percorsi ciclabili (relazioni type=route, route=bicycle|mtb): una riga per ogni strada del percorso, con i tag del
-- percorso. Il loro operator (es. "Linea 6 Biciplan Cambio" -> Citta' Metropolitana di Milano) dice chi gestisce le
-- ciclabili che in OSM non hanno un gestore sul tratto.
local percorsi = osm2pgsql.define_relation_table('percorsi', {
    { column = 'way_id', type = 'int8', not_null = true },
    { column = 'tipo', type = 'text' },
    { column = 'nome', type = 'text' },
    { column = 'tags', type = 'jsonb' },
}, { schema = schema, indexes = { { column = 'way_id', method = 'btree' } } })

local confini = osm2pgsql.define_relation_table('confini', {
    { column = 'admin_level', type = 'int2', not_null = true },
    { column = 'nome', type = 'text' },
    { column = 'tags', type = 'jsonb' },
    { column = 'geom', type = 'multipolygon', projection = 4326, not_null = true },
}, { schema = schema })

function osm2pgsql.process_node(object)
    local t = object.tags
    local segnali = (t.traffic_sign or '') .. ';' .. (t['traffic_sign:forward'] or '') .. ';'
        .. (t['traffic_sign:backward'] or '')
    if segnali:find('city_limit', 1, true) then
        punti:insert({ tipo = 'cartello', nome = t.name, tags = t, geom = object:as_point() })
    end
    if t.place and localita[t.place] then
        punti:insert({ tipo = 'localita', nome = t.name, tags = t, geom = object:as_point() })
    end
end

function osm2pgsql.process_way(object)
    local hw = object.tags.highway
    if hw == nil or escluse[hw] then
        return
    end
    vie:insert({ highway = hw, tags = object.tags, geom = object:as_linestring() })
end

function osm2pgsql.process_relation(object)
    local t = object.tags
    if t.type == 'route' and (t.route == 'bicycle' or t.route == 'mtb') then
        for _, m in ipairs(object.members) do
            if m.type == 'w' then
                percorsi:insert({ way_id = m.ref, tipo = t.route, nome = t.name, tags = t })
            end
        end
        return
    end
    if t.boundary ~= 'administrative' then
        return
    end
    local livello = tonumber(t.admin_level)
    -- 4 regione, 6 provincia o citta' metropolitana, 8 comune
    if livello ~= 4 and livello ~= 6 and livello ~= 8 then
        return
    end
    confini:insert({ admin_level = livello, nome = t.name, tags = t, geom = object:as_multipolygon() })
end
