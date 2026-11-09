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

local confini = osm2pgsql.define_relation_table('confini', {
    { column = 'admin_level', type = 'int2', not_null = true },
    { column = 'nome', type = 'text' },
    { column = 'tags', type = 'jsonb' },
    { column = 'geom', type = 'multipolygon', projection = 4326, not_null = true },
}, { schema = schema })

function osm2pgsql.process_way(object)
    local hw = object.tags.highway
    if hw == nil or escluse[hw] then
        return
    end
    vie:insert({ highway = hw, tags = object.tags, geom = object:as_linestring() })
end

function osm2pgsql.process_relation(object)
    local t = object.tags
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
