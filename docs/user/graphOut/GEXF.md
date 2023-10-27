The Graph can be given through the .gexf and through the _light.gexf files. The _light.gexf file will contain the gene families as nodes and the edges between gene families describing their relationship, and the .gexf file will contain the same thing, but also include more informations about each gene and each relation between gene families. 
We have made two different files representing the same graph because, while the non-light file is exhaustive, it can be very heavy to manipulate and most of the information in it are not of interest to everyone. The _light.gexf file should be the one you use to manipulate the pangenome graph most of the time.

They can be manipulated and visualised through a software called [Gephi](https://gephi.org/), with which we have made extensive testings, or potentially any other softwares or libraries that can read gexf files such as [networkx](https://networkx.github.io/documentation/stable/index.html) or [gexf-js](https://github.com/raphv/gexf-js) among others. 

Using Gephi, the layout can be tuned as illustrated below:

![Gephi layout](https://github.com/labgem/PPanGGOLiN/raw/master/images/gephi.gif)

We advise the Gephi "Force Atlas 2" algorithm to compute the graph layout with "Stronger Gravity: on" and "scaling: 4000" but don't hesitate to tinker the layout parameters.

In the _light.gexf file : 
The nodes will contain the number of genes belonging to the gene family, the most commun gene name (if you provided annotations), the most common product name(if you provided annotations), the partitions it belongs to, its average and median size in nucleotids, and the number of organisms that have this gene family.

The edges contain the number of times they are present in the pangenome.

The .gexf non-light file will contain in addition to this all the information about genes belonging to each gene families, their names, their product string, their sizes and all the information about the neighborhood relationships of each pair of genes described through the edges.

The light gexf can be generated using the 'write' subcommand as such : 

`ppanggolin write -p pangenome.h5 --light_gexf`

while the gexf file can be generated as such : 

`ppanggolin write -p pangenome.h5 --gexf`