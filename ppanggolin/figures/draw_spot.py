#!/usr/bin/env python3
# coding:utf-8


# default libraries
import logging
from collections import defaultdict, Counter
import random
from math import pi
import sys

# installed libraries
from scipy.spatial.distance import pdist
from scipy.sparse import csc_matrix
from scipy.cluster.hierarchy import linkage, dendrogram
import networkx as nx

from tqdm import tqdm
from bokeh.plotting import ColumnDataSource, figure, save
from bokeh.io import output_file
from bokeh.layouts import column, row
from bokeh.models import WheelZoomTool, LabelSet, Slider, CustomJS, HoverTool, RadioGroup, Div, Column, GlyphRenderer

# local libraries
from ppanggolin.pangenome import Pangenome
from ppanggolin.region import Spot
from ppanggolin.utils import jaccard_similarities
from ppanggolin.formats import check_pangenome_info
from ppanggolin.RGP.spot import comp_border


def check_predicted_spots(pangenome):
    """ checks pangenome status and .h5 files for predicted spots, raises an error if they were not predicted"""
    if pangenome.status["spots"] == "No":
        raise Exception("You are trying to draw spots for a pangenome that does not have spots predicted. "
                        "Please see the 'spot' subcommand.")


def make_colors_for_iterable(it: set) -> dict:
    """
    Randomly picks a color for all elements of a given iterable

    :param it: Iterable families or modules

    :return: Dictionary with for each element a random color associate
    """

    famcol = {}
    for element in it:
        col = list(random.choices(range(256), k=3))
        if element == "none":
            famcol[element] = "#D3D3D3"
        else:
            famcol[element] = '#%02x%02x%02x' % (col[0], col[1], col[2])
    return famcol


def order_gene_lists(gene_lists: list, overlapping_match: int, exact_match: int, set_size: int):
    """
    Order all rgps the same way, and order them by similarity in gene content.

    :param gene_lists: List of genes in rgps
    :param overlapping_match: Allowed number of missing persistent genes when comparing flanking genes
    :param exact_match: Number of perfectly matching flanking single copy markers required to associate RGPs
    :param set_size: Number of single copy markers to use as flanking genes for RGP

    :return: List of ordered genes
    """
    line_order_gene_lists(gene_lists, overlapping_match, exact_match, set_size)
    return row_order_gene_lists(gene_lists)


def row_order_gene_lists(gene_lists: list) -> list:
    """
    Row ordering of all rgps

    :param gene_lists:

    :return : An ordered genes list
    """
    fam_dict = defaultdict(set)
    #if there is only one, ordering is useless
    if len(gene_lists) == 1:
        return gene_lists

    if len(gene_lists) > sys.getrecursionlimit():
        sys.setrecursionlimit(len(gene_lists))#we need the recursion limit to be higher than the number of regions.

    for index, genelist in enumerate([genelist[0] for genelist in gene_lists]):
        for gene in genelist:
            if hasattr(gene, "family"):
                fam_dict[gene.family].add(index)
    all_indexes = []
    all_columns = []
    data = []
    for famIndex, RGPindexes in enumerate(fam_dict.values()):
        all_indexes.extend([famIndex] * len(RGPindexes))
        all_columns.extend(RGPindexes)
        data.extend([1.0] * len(RGPindexes))

    mat_p_a = csc_matrix((data, (all_indexes, all_columns)), shape=(len(fam_dict), len(gene_lists)), dtype='float')
    dist = pdist(1 - jaccard_similarities(mat_p_a, 0).todense())
    hc = linkage(dist, 'single')

    dendro = dendrogram(hc, no_plot=True)

    new_gene_lists = [gene_lists[index] for index in dendro["leaves"]]

    return new_gene_lists


def line_order_gene_lists(gene_lists: list, overlapping_match: int, exact_match: int, set_size: int):
    """
    Line ordering of all rgps

    :param gene_lists: list
    :param overlapping_match: Allowed number of missing persistent genes when comparing flanking genes
    :param exact_match: Number of perfectly matching flanking single copy markers required to associate RGPs
    :param set_size: Number of single copy markers to use as flanking genes for RGP
    """
    classified = {0}  # first gene list has the right order
    new_classify = set()

    to_classify = set(range(1, len(gene_lists)))  # the others may (or may not) have it

    while len(to_classify) != 0:
        for classIndex in classified:
            base_border1 = [gene.family for gene in gene_lists[classIndex][1][0]]
            base_border2 = [gene.family for gene in gene_lists[classIndex][1][1]]
            for unclassIndex in list(to_classify):
                border1 = [gene.family for gene in gene_lists[unclassIndex][1][0]]
                border2 = [gene.family for gene in gene_lists[unclassIndex][1][1]]
                if comp_border(base_border1, border1, overlapping_match, set_size, exact_match) and \
                        comp_border(base_border2, border2, overlapping_match, set_size, exact_match):
                    to_classify.discard(unclassIndex)
                    new_classify.add(unclassIndex)
                elif comp_border(base_border2, border1, overlapping_match, set_size, exact_match) and \
                        comp_border(base_border1, border2, overlapping_match, set_size, exact_match):
                    # reverse the order of the genes to match the 'reference'
                    gene_lists[unclassIndex][0] = gene_lists[unclassIndex][0][::-1]
                    # inverse the borders
                    former_border_1 = gene_lists[unclassIndex][1][0]
                    former_border_2 = gene_lists[unclassIndex][1][1]
                    gene_lists[unclassIndex][1][0] = former_border_2
                    gene_lists[unclassIndex][1][1] = former_border_1

                    # specify the new 'classified' and remove from unclassified
                    to_classify.discard(unclassIndex)
                    new_classify.add(unclassIndex)
        classified |= new_classify  # the newly classified will help to check the unclassified,
        # the formerly classified are not useful for what remains (if something remains)
        new_classify = set()


def subgraph(spot: Spot, outname: str, with_border: bool = True, set_size: int = 3,
             multigenics: set = None, fam_to_mod: dict = None):
    """
    Write a pangeome subgraph of the gene families of a spot in gexf format

    :param spot:
    :param outname:
    :param with_border:
    :param set_size:
    :param multigenics:
    :param fam_to_mod:
    """
    g = nx.Graph()

    for rgp in spot.regions:
        if with_border:
            borders = rgp.get_bordering_genes(set_size, multigenics)
            minpos = min([gene.position for border in borders for gene in border])
            maxpos = max([gene.position for border in borders for gene in border])
        else:
            minpos = rgp.start_gene.position
            maxpos = rgp.stop_gene.position
        gene_list = rgp.contig.genes[minpos:maxpos + 1]
        prev = None
        for gene in gene_list:
            g.add_node(gene.family.name, partition=gene.family.named_partition)
            if fam_to_mod is not None:
                curr_mod = fam_to_mod.get(gene.family)
                if curr_mod is not None:
                    g.nodes[gene.family.name]["module"] = curr_mod
            try:
                g.nodes[gene.family.name]["occurrence"] += 1
            except KeyError:
                g.nodes[gene.family.name]["occurrence"] = 1
            if gene.name != "":
                if "name" in g.nodes[gene.family.name]:
                    try:
                        g.nodes[gene.family.name]["name"][gene.name] += 1
                    except KeyError:
                        g.nodes[gene.family.name]["name"][gene.name] = 1
                else:
                    g.nodes[gene.family.name]["name"] = Counter([gene.name])
            if prev is not None:
                g.add_edge(gene.family.name, prev)
                try:
                    g[gene.family.name][prev]["rgp"].add(rgp)
                except KeyError:
                    g[gene.family.name][prev]["rgp"] = set(rgp)
            prev = gene.family.name
    for node1, node2 in g.edges:
        g[node1][node2]["weight"] = len(g[node1][node2]["rgp"]) / len(spot.regions)
        del g[node1][node2]["rgp"]
    for node in g.nodes:
        if "name" in g.nodes[node]:
            g.nodes[node]["name"] = g.nodes[node]["name"].most_common(1)[0][0]

    nx.write_gexf(g, outname)


def mk_source_data(genelists: list, fam_col: dict, fam_to_mod: dict) -> (ColumnDataSource, list):
    """

    :param genelists:
    :param fam_col: Dictionary with for each family the corresponding color
    :param fam_to_mod: Dictionary with the correspondance modules families
    :return:
    """
    partition_colors = {"shell": "#00D860", "persistent": "#F7A507", "cloud": "#79DEFF"}

    df = {'name': [], 'ordered': [], 'strand': [], "start": [], "stop": [], "length": [], 'module': [],
          'module_color': [], 'x': [], 'y': [], 'width': [], 'family_color': [], 'partition_color': [], 'partition': [],
          "family": [], "product": [], "x_label": [], "y_label": [], "label": [], "gene_type": [], 'gene_ID': [],
          "gene_local_ID": []}

    for index, GeneList in enumerate(genelists):
        genelist = GeneList[0]

        if genelist[0].start < genelist[1].start:
            # if the order has been inverted, positionning elements on the figure is different
            ordered = True
            start = genelist[0].start
        else:
            ordered = False
            start = genelist[0].stop

        for gene in genelist:
            df["ordered"].append(str(ordered))
            df["strand"].append(gene.strand)
            df["start"].append(gene.start)
            df["stop"].append(gene.stop)
            df["length"].append(max([gene.stop, gene.start])-min([gene.stop, gene.start]))
            df["gene_type"].append(gene.type)
            df["product"].append(gene.product)
            df["gene_local_ID"].append(gene.local_identifier)
            df['gene_ID'].append(gene.ID)

            if "RNA" in gene.type:  # dedicated values for RNA genes
                df["name"].append(gene.product)
                df["family"].append(gene.type)
                df["partition"].append("none")
                df["family_color"].append("#A109A7")
                df["partition_color"].append("#A109A7")
                df["module"].append("none")
            else:
                df["name"].append(gene.name)
                df["family"].append(gene.family.name)
                df["partition"].append(gene.family.named_partition)
                df["family_color"].append(fam_col[gene.family])
                df["partition_color"].append(partition_colors[gene.family.named_partition])
                df["module"].append(fam_to_mod.get(gene.family, "none"))

            df["x"].append((abs(gene.start - start) + abs(gene.stop - start)) / 2)
            df["width"].append(gene.stop - gene.start)
            df["x_label"].append(str(int(df["x"][-1]) - int(int(df["width"][-1]) / 2)))
            if ordered:
                if gene.strand == "+":
                    df["y"].append((index * 10) + 1)
                else:
                    df["y"].append((index * 10) - 1)
            else:
                if gene.strand == "+":
                    df["y"].append((index * 10) - 1)
                else:
                    df["y"].append((index * 10) + 1)
            df["y_label"].append(float(df["y"][-1]) + 1.5)
    df["label"] = df["name"]
    df["line_color"] = df["partition_color"]
    df["fill_color"] = df["family_color"]

    # define colors for modules
    mod2col = make_colors_for_iterable(set(df["module"]))
    mod_colors = []
    for mod in df["module"]:
        mod_colors.append(mod2col[mod])
    df["module_color"] = mod_colors

    # defining things that we will see when hovering over the graphical elements
    tooltips = [
        ("start", "@start"),
        ("stop", "@stop"),
        ("length", "@length"),
        ("name", "@name"),
        ("product", "@product"),
        ("family", "@family"),
        ("module", "@module"),
        ("partition", "@partition"),
        ("local identifier", "@gene_local_ID"),
        ("gene ID", "@gene_ID"),
        ("ordered", "@ordered"),
        ("strand", "@strand"),
    ]

    return ColumnDataSource(data=df), tooltips


def add_gene_tools(recs: GlyphRenderer, source_data: ColumnDataSource) -> Column:
    """
    Define tools to change the outline and fill colors of genes

    :param recs:
    :param source_data:
    :return:
    """

    def color_str(color_element: str) -> str:
        """ Javascript code to switch between partition, family and module color for the given 'color_element'

        :param color_element:

        :return: Javascript code
        """
        return f"""
            if(this.active == 0){{
                source.data['{color_element}'] = source.data['partition_color'];
            }}else if(this.active == 1){{
                source.data['{color_element}'] = source.data['family_color'];
            }}else if(this.active == 2){{
                source.data['{color_element}'] = source.data['module_color'];
            }}
            recs.{color_element} = source.data['{color_element}'];
            source.change.emit();
        """

    radio_line_color = RadioGroup(labels=["partition", "family", "module"], active=0)
    radio_fill_color = RadioGroup(labels=["partition", "family", "module"], active=1)

    radio_line_color.js_on_click(CustomJS(args=dict(recs=recs, source=source_data),
                                          code=color_str("line_color")))

    radio_fill_color.js_on_click(CustomJS(args=dict(recs=recs, source=source_data),
                                          code=color_str("fill_color")))

    color_header = Div(text="<b>Genes:</b>")
    line_title = Div(text="""Color to use for gene outlines:""",
                     width=200, height=100)
    fill_title = Div(text="""Color to fill genes with:""",
                     width=200, height=100)

    gene_outline_size = Slider(start=0, end=10, value=5, step=0.1, title="Gene outline size:")
    gene_outline_size.js_on_change('value', CustomJS(args=dict(other=recs),
                                                     code="""
                other.glyph.line_width = this.value;
                """
                                                     ))

    return column(color_header, row(column(line_title, radio_line_color), column(fill_title, radio_fill_color)),
                  gene_outline_size)


def add_gene_labels(fig, source_data: ColumnDataSource) -> (Column, LabelSet):
    """

    :param fig:
    :param source_data:
    :return:
    """
    labels = LabelSet(x='x_label', y='y_label', text='label', source=source_data, render_mode='canvas',
                      text_font_size="18px")
    slider_font = Slider(start=0, end=64, value=16, step=1, title="Gene label font size in px")
    slider_angle = Slider(start=0, end=pi / 2, value=0, step=0.01, title="Gene label angle in radian")

    radio_label_type = RadioGroup(labels=["name", "product", "family", "local identifier", "gene ID", "none"],
                                  active=0)

    slider_angle.js_link('value', labels, 'angle')

    slider_font.js_on_change('value',
                             CustomJS(args=dict(other=labels),
                                      code="other.text_font_size = this.value+'px';"
                                      )
                             )

    radio_label_type.js_on_click(CustomJS(args=dict(other=labels, source=source_data),
                                          code="""
                if(this.active == 5){
                    source.data['label'] = [];
                    for(var i=0;i<source.data['name'].length;i++){
                        source.data['label'].push('');
                    }
                }else if(this.active == 3){
                    source.data['label'] = source.data['gene_local_ID'];
                }else if(this.active == 4){
                    source.data['label'] = source.data['gene_ID'];
                }
                else{
                    source.data['label'] = source.data[this.labels[this.active]];
                }
                other.source = source;
                source.change.emit();
                """
                                          ))

    label_header = Div(text="<b>Gene labels:</b>")
    radio_title = Div(text="""Gene labels to use:""",
                      width=200, height=100)
    labels_block = column(label_header, row(slider_font, slider_angle), column(radio_title, radio_label_type))

    fig.add_layout(labels)

    return labels_block, labels


def mk_genomes(gene_lists: list, ordered_counts: list) -> (ColumnDataSource, list):
    """

    :param gene_lists:
    :param ordered_counts:
    :return:
    """
    df = {"name": [], "width": [], "occurrences": [], 'x': [], 'y': [], "x_label": []}

    for index, GeneList in enumerate(gene_lists):
        genelist = GeneList[0]
        df["occurrences"].append(ordered_counts[index])
        df["y"].append(index * 10)
        if genelist[0].start < genelist[1].start:
            # if the order has been inverted, positionning elements on the figure is different
            df["width"].append(abs(genelist[-1].stop - genelist[0].start))
        else:
            # order has been inverted
            df["width"].append(abs(genelist[0].stop - genelist[-1].start))
        df["x"].append((df["width"][-1]) / 2)
        df["x_label"].append(0)
        df["name"].append(genelist[0].organism.name)
    tooltip = [
        ("name", "@name"),
        ("occurrences", "@occurrences"),
    ]
    return ColumnDataSource(data=df), tooltip


def add_genome_tools(fig, gene_recs: GlyphRenderer, genome_recs: GlyphRenderer, gene_source: ColumnDataSource,
                     genome_source: ColumnDataSource, nb: int, gene_labels: LabelSet):
    """

    :param fig:
    :param gene_recs:
    :param genome_recs:
    :param gene_source:
    :param genome_source:
    :param nb:
    :param gene_labels:
    :return:
    """
    # add genome labels
    genome_labels = LabelSet(x='x_label', y='y', x_offset=-20, text='name', text_align="right", source=genome_source,
                             render_mode='canvas', text_font_size="16px")
    fig.add_layout(genome_labels)

    slider_font = Slider(start=0, end=64, value=16, step=1, title="Genome label font size in px")
    slider_font.js_on_change('value',
                             CustomJS(args=dict(other=genome_labels),
                                      code="other.text_font_size = this.value+'px';"
                                      )
                             )

    slider_offset = Slider(start=-400, end=0, value=-20, step=1, title="Genome label offset")
    slider_offset.js_link('value', genome_labels, 'x_offset')

    slider_spacing = Slider(start=1, end=40, value=10, step=1, title="Genomes spacing")
    slider_spacing.js_on_change('value', CustomJS(
        args=dict(gene_recs=gene_recs, gene_source=gene_source, genome_recs=genome_recs, genome_source=genome_source,
                  nb_elements=nb, genome_labels=genome_labels, gene_labels=gene_labels),
        code="""
            var current_val = genome_source.data['y'][genome_source.data['y'].length - 1] / (nb_elements-1);
            for (let i=0 ; i < genome_source.data['y'].length ; i++){
                genome_source.data['y'][i] =  (genome_source.data['y'][i] * this.value) / current_val;
            }
            for (let i=0 ; i < gene_source.data['y'].length ; i++){
                if((gene_source.data['ordered'][i] == 'True' && gene_source.data['strand'][i] == '+') || (gene_source.data['ordered'][i] == 'False' && gene_source.data['strand'][i] == '-') ){
                    gene_source.data['y'][i] = (((gene_source.data['y'][i]-1) * this.value) / current_val) +1;
                    gene_source.data['y_label'][i] = (((gene_source.data['y_label'][i]-1-1.5) * this.value) / current_val) + 1 + 1.5;
                }else{
                    gene_source.data['y'][i] = (((gene_source.data['y'][i]+1) * this.value) / current_val) -1;
                    gene_source.data['y_label'][i] = (((gene_source.data['y_label'][i]+1-1.5) * this.value) / current_val) -1 + 1.5;

                }
            }
            gene_recs.source = gene_source;
            genome_recs.source = genome_source;
            gene_labels.source = gene_source;
            genome_labels.source = genome_source;
            gene_source.change.emit();
            genome_source.change.emit();
        """))

    genome_header = Div(text="<b>Genomes:</b>")
    return column(genome_header, slider_spacing, slider_font, slider_offset)


def draw_curr_spot(gene_lists: list, ordered_counts: list, fam_to_mod: dict, fam_col: dict, file_name: str):
    """

    :param gene_lists:
    :param ordered_counts:
    :param fam_to_mod:
    :param fam_col: Dictionnary with for each family the corresponding color
    :param file_name:
    :return:
    """

    # Prepare the source data
    output_file(file_name + ".html")

    # generate the figure and add some tools to it
    wheel_zoom = WheelZoomTool()
    fig = figure(title="spot graphic", plot_width=1600, plot_height=600,
                 tools=["pan", "box_zoom", "reset", "save", wheel_zoom, "ywheel_zoom", "xwheel_zoom"])
    fig.axis.visible = True
    fig.toolbar.active_scroll = wheel_zoom

    # genome rectangles
    genome_source, genome_tooltip = mk_genomes(gene_lists, ordered_counts)
    genome_recs = fig.rect(x='x', y='y', fill_color="dimgray", width="width", height=0.5, source=genome_source)
    genome_recs_hover = HoverTool(renderers=[genome_recs], tooltips=genome_tooltip, mode="mouse",
                                  point_policy="follow_mouse")
    fig.add_tools(genome_recs_hover)

    # gene rectanges
    gene_source, gene_tooltips = mk_source_data(gene_lists, fam_col, fam_to_mod)
    recs = fig.rect(x='x', y='y', line_color='line_color', fill_color='fill_color', width='width', height=2,
                    line_width=5, source=gene_source)
    recs_hover = HoverTool(renderers=[recs], tooltips=gene_tooltips, mode="mouse", point_policy="follow_mouse")
    fig.add_tools(recs_hover)
    # gene modification tools
    gene_tools = add_gene_tools(recs, gene_source)

    # label modification tools
    labels_tools, labels = add_gene_labels(fig, gene_source)

    # genome tool
    genome_tools = add_genome_tools(fig, recs, genome_recs, gene_source, genome_source, len(gene_lists), labels)

    save(column(fig, row(labels_tools, gene_tools), row(genome_tools)))


def draw_selected_spots(selected_spots: list, pangenome: Pangenome, output: str, overlapping_match: int,
                        exact_match: int, set_size: int, disable_bar: bool = False):
    """
    Draw only the selected spot and give parameters

    :param selected_spots: List of the selected spot by user
    :param pangenome: Pangenome containing spot
    :param output: Path to output directory
    :param overlapping_match: Allowed number of missing persistent genes when comparing flanking genes
    :param exact_match:
    :param set_size:
    :param disable_bar: Allow preventing bar progress print
    """

    logging.getLogger().info("Ordering genes among regions, and drawing spots...")

    multigenics = pangenome.get_multigenics(pangenome.parameters["RGP"]["dup_margin"])

    fam2mod = {}
    for mod in pangenome.modules:
        for fam in mod.families:
            fam2mod[fam] = f"module_{mod.ID}"

    for spot in tqdm(selected_spots, total=len(selected_spots), unit="spot", disable=disable_bar):

        fname = output + '/spot_' + str(spot.ID)

        # write rgps representatives and the rgps they are identical to
        out_struc = open(fname + '_identical_rgps.tsv', 'w')
        out_struc.write('representative_rgp\trepresentative_rgp_organism\tidentical_rgp\tidentical_rgp_organism\n')
        for keyRGP, otherRGPs in spot.get_uniq_to_rgp().items():
            for rgp in otherRGPs:
                out_struc.write(f"{keyRGP.name}\t{keyRGP.organism.name}\t{rgp.name}\t{rgp.organism.name}\n")
        out_struc.close()

        fams = set()
        gene_lists = []

        for rgp in spot.regions:
            borders = rgp.get_bordering_genes(set_size, multigenics)
            minpos = min([gene.position for border in borders for gene in border])
            maxpos = max([gene.position for border in borders for gene in border])
            gene_list = rgp.contig.genes[minpos:maxpos + 1]
            minstart = min([gene.start for border in borders for gene in border])
            maxstop = max([gene.stop for border in borders for gene in border])
            rnas_toadd = set()
            for rna in rgp.contig.RNAs:
                if minstart < rna.start < maxstop:
                    rnas_toadd.add(rna)
            gene_list.extend(rnas_toadd)
            gene_list = sorted(gene_list, key=lambda x: x.start)

            fams |= {gene.family for gene in gene_list if gene.type == "CDS"}

            gene_lists.append([gene_list, borders, rgp])
        famcolors = make_colors_for_iterable(fams)
        # order all rgps the same way, and order them by similarity in gene content
        gene_lists = order_gene_lists(gene_lists, overlapping_match, exact_match, set_size)

        count_uniq = spot.count_uniq_ordered_set()

        # keep only the representative rgps for the figure
        uniq_gene_lists = []
        ordered_counts = []
        for genelist in gene_lists:
            curr_genelist_count = count_uniq.get(genelist[2], None)
            if curr_genelist_count is not None:
                uniq_gene_lists.append(genelist)
                ordered_counts.append(curr_genelist_count)

        draw_curr_spot(uniq_gene_lists, ordered_counts, fam2mod, famcolors, fname)
        subgraph(spot, fname + ".gexf", set_size=set_size, multigenics=multigenics, fam_to_mod=fam2mod)
    logging.getLogger().info(f"Done drawing spot(s), they can be found in the directory: '{output}'")


def draw_spots(pangenome: Pangenome, output: str, spot_list: str, disable_bar: bool = False):
    """
    Main function to draw spot

    :param pangenome: Pangenome with spot predicted
    :param output: Path to output directory
    :param spot_list: List of spot to draw
    :param disable_bar: Allow to disable progress bar
    """
    # check that the pangenome has spots
    check_predicted_spots(pangenome)

    need_mod = False
    if pangenome.status["modules"] != "No":
        # modules are not required to be loaded, but if they have been computed we load them.
        need_mod = True

    check_pangenome_info(pangenome, need_annotations=True, need_families=True, need_graph=False, need_partitions=True,
                         need_rgp=True, need_spots=True, need_modules=need_mod, disable_bar=disable_bar)

    if spot_list == 'all' or any(x == 'all' for x in spot_list):
        logging.getLogger().debug(f"'all' value is found in spot list, all spots are drawn.")
        selected_spots = pangenome.spots
    elif spot_list == "synteny" or any(x == 'synteny' for x in spot_list):
        logging.getLogger().debug(f"'synteny' value is found in spot list, all spots with more than 1 conserved synteny are drawn.")
        selected_spots = [s for s in pangenome.spots if len(s.get_uniq_ordered_set()) > 1]
    else:
        curated_spot_list = {'spot_' + str(s) if not s.startswith("spot_") else str(s) for s in spot_list}
        logging.getLogger().debug(f'Required spots to draw: {curated_spot_list}')
        selected_spots = [s for s in pangenome.spots if "spot_" + str(s.ID) in curated_spot_list]
        if len(selected_spots) != len(curated_spot_list):
            existing_spots = {"spot_" + str(s.ID) for s in pangenome.spots} 
            required_non_existing_spots = curated_spot_list - existing_spots
            logging.getLogger().warning(f'{len(required_non_existing_spots)} required spots to draw do not exist: {" ".join(required_non_existing_spots)} ')

    if len(selected_spots) < 10:
        logging.getLogger().info(f"Drawing the following spots: "
                                 f"{' '.join(['spot_' + str(s.ID) for s in selected_spots])}")
    else:
        logging.getLogger().info(f"Drawing {len(selected_spots)} spots")

    draw_selected_spots(selected_spots, pangenome, output,
                        overlapping_match=pangenome.parameters["spots"]["overlapping_match"],
                        exact_match=pangenome.parameters["spots"]["exact_match"],
                        set_size=pangenome.parameters["spots"]["set_size"], disable_bar=disable_bar)
