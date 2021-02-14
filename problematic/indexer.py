from scipy import ndimage
import heapq
import lmfit
import numpy as np
import matplotlib.pyplot as plt
from skimage import morphology

from collections import namedtuple
from collections import OrderedDict

from .stretch_correction import affine_transform_ellipse_to_circle, apply_transform_to_image
from .tools import find_beam_center
from .get_score_cy import get_score, get_score_mod, get_score_shape, get_score_shape_lst
from .projector import Projector

import logging
logger = logging.getLogger(__name__)


def get_intensities(img, result, projector, radius=1):
    """
    Grab reflection intensities at given projection

    radius: int, optional
        Search for largest point in defined radius around projected peak positions
    """
    proj = projector.get_projection(result.alpha, result.beta, result.gamma)
    i, j, hkl = get_indices(proj[:,3:5], result.scale, (result.center_x, result.center_y), img.shape, hkl=proj[:,0:3])
    if radius > 1:
        footprint = morphology.disk(radius)
        img = ndimage.maximum_filter(img, footprint=footprint)
    inty = img[i, j].reshape(-1,1)
    return np.hstack((hkl, inty, np.ones_like(inty))).astype(int)


def standardize_indices(arr, cell, key=None):
    """
    TODO: add to xcore.spacegroup.SpaceGroup

    Standardizes reflection indices
    From Siena Computing School 2005, Reciprocal Space Tutorial (G. Sheldrick)
    http://www.iucr.org/resources/commissions/crystallographic-computing/schools
        /siena-2005-crystallographic-computing-school/speakers-notes
    """
    stacked_symops = np.stack([s.r for s in cell.symmetry_operations_p])
    
    m = np.dot(arr, stacked_symops).astype(int)
    m = np.hstack([m, -m])
    i = np.lexsort(m.transpose((2,0,1)))
    merged =  m[np.arange(len(m)), i[:,-1]] # there must be a better way to index this, but this works and is quite fast

    return merged


def get_score_py(img, pks, scale, center_x, center_y):
    """Equivalent of get_score implemented in python"""
    xmax = img.shape[0]
    ymax = img.shape[1]
    xmin = 0
    ymin = 0
    nrows = pks.shape[0]
    score = 0 
    
    for n in range(nrows):
        i = int(pks[n, 0] * scale + center_x)
        j = int(pks[n, 1] * scale + center_y)
        
        if j < ymin:
            continue
        if j >= ymax:
            continue
        if i < xmin:
            continue
        if i >= xmax:
            continue

        score = score + img[i, j]

    return score


def remove_background_gauss(img, min_sigma=3, max_sigma=30, threshold=1):
    """Remove background from an image using a difference of gaussian approach

    img: ndarray
        Image array
    min_sigma: float, optional
        The minimum standard deviation for the gaussian filter
    max_sigma: float, optional
        The maximum standard deviation for the gaussian filter
    threshold: float, optional
        Remove any remaining features below this threshold

    Returns img: ndarray
        Image array with background removed
    """
    img_float = img.astype(float)
    img_corr = np.maximum(ndimage.gaussian_filter(img_float, min_sigma) - ndimage.gaussian_filter(img_float, max_sigma) - threshold, 0)
    return img_corr.astype(int)


def make_2d_rotmat(theta):
    """Take angle in radians, and return 2D rotation matrix"""
    R = np.array([[np.cos(theta), -np.sin(theta)],
                  [np.sin(theta),  np.cos(theta)]])
    return R


def get_indices(pks, scale, center, shape, hkl=None):
    """Get the pixel indices for an image"""
    shapex, shapey = shape
    i, j = (pks * scale + center).astype(int).T
    sel = (0 < j) & (j < shapey) & (0 < i) & (i < shapex)
    if hkl is None:
        return i[sel], j[sel]
    else:
        return i[sel], j[sel], hkl[sel]


# store the results of indexing
IndexingResult = namedtuple("IndexingResult", ["score", "number", "alpha", "beta", "gamma", "center_x", "center_y", "scale", "phase"])
IndexingResultDType = [("score", '<f8'), ("number", '<i8'), ("alpha", '<f8'), ("beta", '<f8'), ("gamma", '<f8'), 
                ("center_x", '<f8'), ("center_y", '<f8'), ("scale", '<f8'), ("phase", '<S16')]

# description of each projection
ProjInfo = namedtuple("ProjectionInfo", ["number", "alpha", "beta"])


class IndexerMulti(object):
    """
    Indexing class for serial snapshot crystallography. Find the crystal orientations 
    from a single electron diffraction snapshot using a brute force method

    IndexerMulti allows multiple indexers to be stored for dealing with multiphase problems

    indexers: dict
        dictionary of indexers to use, the key is used as the identifier in the IndexingResult

    For more information see: Indexer()
    """
    def __init__(self, indexers={}):
        super(IndexerMulti, self).__init__()
        
        self._indexers = indexers

    @classmethod
    def from_cells(cls, cells, pixelsize, **kwargs):
        indexers = {}
        for cell in cells:
            phase = cell["name"]
            projector = Projector.from_parameters({**cell, **kwargs})
            indexer = Indexer.from_projector(projector, pixelsize=pixelsize)
            indexers[phase] = indexer

        return cls(indexers)

    def set_pixelsize(self, pixelsize):
        """
        Sets the pixelsize for all indexers
        """
        for phase, indexer in self._indexers.items():
            indexer.set_pixelsize(pixelsize)

    def to_dict(self):
        return self.to_list()

    def to_list(self):
        return [indexer.to_dict() for indexer in self._indexers]

    def index(self, img, center, **kwargs):
        """
        Applied all indexers to img
        """

        nsolutions = kwargs.get("nsolutions", 20)

        results = []
        for phase, indexer in self._indexers.items():
            res = indexer.index(img, center, phase=phase, **kwargs)

            # scale score by cell volume as an attempt to normalize the scores
            # scores are consistent within an indexer class, but difficult to compare between indexers
            # I suspect smaller unit cells have an advantage over larger ones (because they hit more
            #     0-pixels using get_score_mod)
            res = [r._replace(score=r.score*(indexer.projector.cell.volume/5000)) for r in res]
            results.extend(res)

        return sorted(results, key=lambda t: t.score, reverse=True)[0:nsolutions]

    def refine_all(self, img, results, sort=True, **kwargs):
        """
        Optimizes the given solutions using a least-squares minimization.
        """
        kwargs.setdefault("verbose", False)
        
        refined = []

        for result in results:
            res = self.refine(img, result, **kwargs)
            refined.append(res)

        if sort:
            return sorted(refined, key=lambda t: t.score, reverse=True)
        else:
            return refined

    def refine(self, img, result, **kwargs):
        """
        Optimizes the given solution using a least-squares minimization.
        """
        phase = result.phase
        indexer = self._indexers[phase]
        res = indexer.refine(img, result, phase=phase, **kwargs)
        res = res._replace(score=res.score*(indexer.projector.cell.volume/5000)) # see comment on .index
        return res

    def plot_all(self, img, results, **kwargs):
        """
        Plot each projection given in results on the image
        """
        for result in results:
            self.plot(img, result, **kwargs)
    
    def plot(self, img, result, *args, **kwargs):
        """
        Plot the image with the projection given in 'result'
        """
        phase = result.phase
        indexer = self._indexers[phase]

        indexer.plot(img, result, *args, **kwargs)

    def get_projection(self, result):
        """
        Get projection along a particular zone axis
        See Projector.get_projection
        """
        phase = result.phase
        return self._indexers[phase].get_projection(result)

    def get_intensities(self, img, result, **kwargs):
        """
        Grab reflection intensities at given projection
        See get_intensities
        """
        phase = result.phase
        return self._indexers[phase].get_intensities(img, result, **kwargs)


class Indexer(object):
    """Indexing class for serial snapshot crystallography. Find the crystal orientations 
    from a single electron diffraction snapshot using a brute force method 

    The projections of all possible crystal orientations are generated in advance.
    For each projection, the corresponding value at the x,y coordinates of each
    diffraction spot are summed. The idea is that the best fitting orientation has 
    the highest score.

    Based on http://journals.iucr.org/m/issues/2015/04/00/fc5010/index.html

    projections: tuple or list of ndarrays
        List of numpy arrays with 5 columns, h, k, l, x, y
        x and y are the reciprocal lattice vectors of the reflections in diffracting
        condition. See Projector.
    infos: tuple or list of ProjInfo objects
        List of information corresponding to the projections
        Contains (n, alpha, beta)
    pixelsize: float
        the dimensions of each pixel expressed in Angstrom^-1. The detector is placed
        at the origin of the reciprocal lattice, with the incident beam perpendicular 
        to the detector.
    theta: float
        Angle step in radians for gamma
    projector: instance of Projector
        Used for generating projections of arbitrary orientation.

    To use:
        img = read_tiff("image_0357_0001.tiff")
        projector = Projector.from_parameters((5.4301,), spgr="Fd-3m")
        indexer = Indexer.from_projector(projector, pixelsize=0.00316)
        results = indexer.index(img, center=(256, 256))
        indexer.plot(results[0])
        refined = indexer.refine_all(img, results)
        indexer.plot(refined[0])
    """
    def __init__(self, projections, infos, pixelsize, theta=0.03, projector=None):
        super(Indexer, self).__init__()
        
        self.projections = projections
        self.infos = infos
        
        self._projector = projector
        
        self.pixelsize = pixelsize
        self.scale = 1/pixelsize
        
        self.theta = theta
        self.minimum_reflections_in_projection = 10

        nprojections = len(self.projections)
        nrotations = int(2*np.pi/self.theta)
        print("{} projections x {} rotations = {} items\n".format(nprojections, nrotations, nprojections*nrotations))
    
    def set_pixelsize(self, pixelsize):
        """
        Sets pixelsize and calculates scale from pixelsize
        """
        self.pixelsize = pixelsize
        self.scale = 1/pixelsize

    def to_dict(self):
        d = self.projector.to_dict()
        d["experiment"] = {
            "pixelsize": self.pixelsize
            }
        return d

    @classmethod
    def from_projections_file(cls, fn="projections.npy", **kwargs):
        """
        Initialize instance of Indexing using a projections file

        fn: str
            path to projections.npy
        """
        infos, projections = np.load(fn)
        infos = [ProjInfo(*info) for info in infos]

        return cls(projections=projections, infos=infos, **kwargs)
    
    @classmethod
    def from_projector(cls, projector, **kwargs):
        """
        Initialize isntance of Indexer using an instance of Projector

        projector: Projector object
        """
        projections, infos = projector.generate_all_projections()
        infos = [ProjInfo(*info) for info in infos]
        return cls(projections=projections, infos=infos, projector=projector, **kwargs)
    
    @property
    def projector(self):
        if self._projector:
            return self._projector
        else:
            raise AttributeError("Please supply an instance of 'Projector'.")
    
    @projector.setter
    def projector(self, projector):
        self._projector = projector

    def get_score(self, img, result, projector=None):
        if not projector:
            projector = self.projector
        scale = result.scale
        alpha = result.alpha
        beta = result.beta
        gamma = result.gamma
        center_x = result.center_x
        center_y = result.center_y

        proj = projector.get_projection(alpha, beta, gamma)[:,3:6]

        score  = get_score_shape(img, proj, scale, center_x, center_y)

        return score
    
    def index(self, img, center=None, **kwargs):
        """
        This function attempts to index the diffraction pattern in `img`

        img: ndarray
            image array
        center: tuple(x, y)
            x and y coordinates to the position of the primary beam
        """
        if center is None:
            raise ValueError("No beam center supplied")

        return self.find_orientation(img, center, **kwargs)
    
    def find_orientation(self, img, center, **kwargs):
        """
        This function attempts to find the orientation of the crystal in `img`

        img: ndarray
            image array
        center: tuple(x, y)
            x and y coordinates to the position of the primary beam
        """
        theta      = kwargs.get("theta", self.theta)
        nsolutions = kwargs.get("nsolutions", 25)

        phase = kwargs.get("phase", self.projector.cell.name)

        vals  = []
        
        center_x, center_y = center
        scale = self.scale
        
        for n, projection in enumerate(self.projections):
            if len(projection) < self.minimum_reflections_in_projection:
                continue
            best_score = 0
            best_gamma = 0
            proj = projection[:,3:6]

            score, gamma = get_score_shape_lst(img, proj, scale, center_x, center_y)
            if score > best_score:
                best_score = score
                best_gamma = gamma

            vals.append((best_score, n, best_gamma))

        heap = heapq.nlargest(nsolutions, vals)
        self._vals = vals
        
        heap = sorted(heap, reverse=True)[0:nsolutions]
        
        results = [IndexingResult(score=round(score, 2),
                                  number=n,
                                  alpha=round(self.infos[n].alpha, 4),
                                  beta=round(self.infos[n].beta, 4),
                                  gamma=round(gamma, 4),
                                  center_x=center_x,
                                  center_y=center_y,
                                  scale=round(scale, 4),
                                  phase=phase) for (score, n, gamma) in heap]

        return np.array(results, dtype=IndexingResultDType)
    
    def plot_all(self, img, results, **kwargs):
        """
        Plot each projection given in results on the image

        img: ndarray
            image array
        results: tuple or list of IndexingResult objects
        """
        for result in results:
            self.plot(img, result, **kwargs)
    
    def plot(self, img, result, projector=None, show_hkl=False, title="", ax=None, **kwargs):
        """
        Plot the image with the projection given in 'result'

        img: ndarray
            image array
        result: IndexingResult object
        show_hkl: bool, optional
            Show hkl values as text

        """
        n = result.number
        center_x = result.center_x
        center_y = result.center_y
        scale = result.scale
        alpha = result.alpha
        beta = result.beta
        gamma = result.gamma
        score = result.score
        phase = result.phase

        if not ax:
            ax = plt.subplot()
            show = True
        else:
            show = False
        
        vmax = kwargs.get("vmax", 300)

        if not projector:
            projector = self.projector
        
        proj = projector.get_projection(alpha, beta, gamma)
        pks = proj[:,3:5]
        
        i, j, proj = get_indices(pks, scale, (center_x, center_y), img.shape, hkl=proj)
        
        shape_factor = proj[:,5:6].reshape(-1)
        hkl = proj[:,0:3]

        if title:
            title += (" \n")

        ax.imshow(img, vmax=vmax, cmap="gray")
        ax.plot(center_y, center_x, marker="o")
        if show_hkl:
            for idx, (h, k, l) in enumerate(hkl):
                ax.text(j[idx], i[idx], "{:.0f} {:.0f} {:.0f}".format(h, k, l), color="white")
        
        ax.set_title("{}al: {:.2f}, be: {:.2f}, ga: {:.2f}\nscore = {:.1f}, scale = {:.1f}\nproj = {}, phase = {}".format(title, alpha, beta, gamma, score, scale, n, phase))
        ax.scatter(j, i, marker="+", c=shape_factor)
        ax.set_xlim(0, img.shape[0]-1)
        ax.set_ylim(img.shape[1]-1, 0)
        
        if show:
            plt.show()
    
    def refine_all(self, img, results, sort=True, **kwargs):
        """
        Refine the orientations of all solutions in results agains the given image

        img: ndarray
            Image array
        results: tuple or list of IndexingResult objects
            Specifications of the solutions to be refined
        projector: Projector object, optional
            This keyword should be specified if projector is not already an attribute on Indexer,
            or if a different one should be used
        sort: bool, optional
            Sort the result of the refinement
        """
        kwargs.setdefault("verbose", False)
        new_results = [self.refine(img, result, **kwargs) for result in results]

        # sort in descending order by score
        if sort:
            return sorted(new_results, key=lambda t: t.score, reverse=True)
        else:
            return new_results
    
    def refine(self, img, result, projector=None, verbose=True, method="least-squares", fit_tol=0.1,
               vary_center=True, vary_scale=True, vary_alphabeta=True, vary_gamma=True, **kwargs):
        """
        Refine the orientations of all solutions in results agains the given image

        img: ndarray
            Image array
        result: IndexingResult object
            Specifications of the solution to be refined
        projector: Projector object, optional
            This keyword should be specified if projector is not already an attribute on Indexer,
            or if a different one should be used
        method: str, optional
            Minimization method to use, should be one of 'nelder', 'powell', 'cobyla', 'least-squares'
        fit_tol: float
            Tolerance for termination. For detailed control, use solver-specific options.
        """
        if not projector:
            projector = self.projector

        f_kws = kwargs.get("kws", None)
        
        def objfunc(params, img):
            cx = params["center_x"].value
            cy = params["center_y"].value
            al = params["alpha"].value
            be = params["beta"].value
            ga = params["gamma"].value
            sc = params["scale"].value
            
            proj = projector.get_projection(al, be, ga)
            pks = proj[:,3:6]
            score = get_score_shape(img, pks, sc, cx, cy)

            return 1e3/(1+score)

        params = lmfit.Parameters()
        params.add("center_x", value=result.center_x, vary=vary_center, min=result.center_x - 2.0, max=result.center_x + 2.0)
        params.add("center_y", value=result.center_y, vary=vary_center, min=result.center_y - 2.0, max=result.center_y + 2.0)
        params.add("alpha", value=result.alpha, vary=vary_alphabeta)
        params.add("beta",  value=result.beta,  vary=vary_alphabeta)
        params.add("gamma", value=result.gamma, vary=vary_gamma)
        params.add("scale", value=result.scale, vary=vary_scale, min=result.scale*0.8, max=result.scale*1.2)
        
        args = img,

        res = lmfit.minimize(objfunc, params, args=args, method=method, tol=fit_tol, kws=f_kws)

        if verbose:
            lmfit.report_fit(res)
                
        p = res.params
        
        alpha, beta, gamma = [round(p[key].value, 4) for key in ("alpha", "beta", "gamma")]
        scale, center_x, center_y = [round(p[key].value, 2) for key in ("scale", "center_x", "center_y")]
        
        proj = projector.get_projection(alpha, beta, gamma)
        pks = proj[:,3:6]
        
        score = round(get_score_shape(img, pks, scale, center_x, center_y), 2)
        
        # print "Score: {} -> {}".format(int(score), int(score))
        
        refined = IndexingResult(score=score,
                                 number=result.number,
                                 alpha=alpha,
                                 beta=beta,
                                 gamma=gamma,
                                 center_x=center_x,
                                 center_y=center_y,
                                 scale=scale,
                                 phase=result.phase)
        
        return np.array(refined, dtype=IndexingResultDType).view(np.recarray)

    def probability_distribution(self, img, result, projector=None, verbose=True, vary_center=False, vary_scale=True):
        """https://lmfit.github.io/lmfit-py/fitting.html#lmfit.minimizer.Minimizer.emcee

        Calculate posterior probability distribution of parameters"""
        import corner
        import emcee

        if not projector:
            projector = self.projector
        
        def objfunc(params, pks, img):
            cx = params["center_x"].value
            cy = params["center_y"].value
            al = params["alpha"].value
            be = params["beta"].value
            ga = params["gamma"].value
            sc = params["scale"].value
            
            proj = projector.get_projection(al, be, ga)
            pks = proj[:,3:6]
            score = get_score_shape(img, pks, sc, cx, cy)
            
            resid = 1e3/(1+score)
            
            # Log-likelihood probability for the sampling. 
            # Estimate size of the uncertainties on the data
            s = params['f']
            resid *= 1 / s
            resid *= resid
            resid += np.log(2 * np.pi * s**2)
            return -0.5 * np.sum(resid)

        params = lmfit.Parameters()
        params.add("center_x", value=result.center_x, vary=vary_center, min=result.center_x - 2.0, max=result.center_x + 2.0)
        params.add("center_y", value=result.center_y, vary=vary_center, min=result.center_y - 2.0, max=result.center_y + 2.0)
        params.add("alpha", value=result.alpha + 0.01, vary=True, min=result.alpha - 0.1, max=result.alpha + 0.1)
        params.add("beta",  value=result.beta + 0.01,  vary=True, min=result.beta - 0.1, max=result.beta + 0.1)
        params.add("gamma", value=result.gamma + 0.01, vary=True, min=result.gamma - 0.1, max=result.gamma + 0.1)
        params.add("scale", value=result.scale, vary=vary_scale, min=result.scale*0.8, max=result.scale*1.2)
        
        # Noise parameter
        params.add('f', value=1, min=0.001, max=2)
        
        pks_current = projector.get_projection(result.alpha, result.beta, result.gamma)[:,3:5]
        
        args = pks_current, img
        
        mini = lmfit.Minimizer(objfunc, params, fcn_args=args)
        res = mini.emcee(params=params)

        if verbose:
            print("\nMedian of posterior probability distribution")
            print("--------------------------------------------")
            lmfit.report_fit(res)

        # find the maximum likelihood solution
        highest_prob = np.argmax(res.lnprob)
        hp_loc = np.unravel_index(highest_prob, res.lnprob.shape)
        mle_soln = res.chain[hp_loc]
        
        if verbose:
            for i, par in enumerate(res.var_names):
                params[par].value = mle_soln[i]
            print("\nMaximum likelihood Estimation")
            print("-----------------------------")
            print(params)
        
        corner.corner(res.flatchain, labels=res.var_names, truths=[res.params[par].value for par in res.params if res.params[par].vary])
        plt.show()

    def get_projection(self, result):
        """
        Get projection along a particular zone axis
        See Projector.get_projection()
        """
        proj = self.projector.get_projection(result.alpha, result.beta, result.gamma)

    def get_indices(self, result, shape):
        proj = self.projector.get_projection(result.alpha, result.beta, result.gamma)
        pks = proj[:,3:5]
        i, j, proj = get_indices(pks, result.scale, (result.center_x, result.center_y), shape, hkl=proj)
        return np.hstack((proj, i.reshape(-1,1), j.reshape(-1,1)))

    def get_intensities(self, img, result, **kwargs):
        """
        Grab reflection intensities at given projection
        See get_intensities
        """
        hklie = get_intensities(img, result, self.projector, **kwargs)
        hklie[:,0:3] = standardize_indices(hklie[:,0:3], self.projector.cell)
        return hklie
